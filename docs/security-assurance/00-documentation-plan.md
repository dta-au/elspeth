# 00 — Assurance-pack lifecycle and completion plan

**Status:** maintained lifecycle guide · **Product baseline reviewed against:**
`release/0.8.1` @ `49c184508` (2026-09-30) · **Owner:** ELSPETH
maintainer

This document defines how to maintain the reusable ELSPETH security-assurance
baseline and how an individual installation turns that baseline into a
controlled assessment pack. It is a process document, not an assurance,
accreditation or authority-to-operate claim.

## 1. Two-layer model

### 1.1 Product baseline — this public repository

The product baseline records facts that are true of ELSPETH independent of
where it runs:

- product scope, components, flows and trust boundaries;
- controls implemented by the product and their limits;
- responsibilities ELSPETH assigns to maintainers, deployers, users and
  providers;
- reusable threat, privacy, incident, testing and risk methods;
- secure-development and supply-chain practices; and
- evidence that can be inspected or reproduced from the public repository.

The product baseline states safe limitations and non-guarantees. It does not
publish exploit instructions, live exposure details, open-finding narratives,
named accounts, infrastructure identifiers, actual risk acceptances or other
information that would materially help an attacker.

### 1.2 Deployment record — controlled copy

For each installation, copy this folder to the assessment team's controlled
repository and populate every section headed **Deployment record**. Those
sections capture the release and image digest, hosting and network controls,
enabled integrations, identities and role holders, data and classification,
providers and contracts, retention, monitoring, incident contacts, testing,
control applicability and accepted risks.

Do not populate this public repository with facts about the current device,
server or any other installation. Deployment facts and sensitive evidence stay
in the controlled copy. If an assessment discovers a reusable product
correction, apply that correction to this public baseline without copying the
deployment's sensitive facts back with it.

## 2. Completion states

Each document reports three independent states:

- **Product baseline:** whether reusable product content has been reviewed.
- **Deployment template:** whether the controlled-copy fields and completion
  method are ready to use.
- **Deployment record:** always *unpopulated by design* in this repository.

The product baseline is complete only when all of these conditions hold:

1. Every deployment-independent section is complete and factually checked at
   the named commit.
2. Every material control or limitation has a source, test, workflow,
   procedure or record in [16](16-evidence-index.md), cited as `[EV-nnn]`.
3. No generic placeholder remains. Intentional installation blanks use the
   exact marker `DEPLOYMENT-TODO:` and appear only under a heading containing
   **Deployment record**.
4. Scope, responsibility, threat, privacy, incident, testing and risk methods
   tell a deployment author what a complete record must contain.
5. Relative links and section anchors resolve, evidence identifiers are unique
   and defined, and the validation checks in § 5 pass.
6. The public pack contains no hostname, account identifier, endpoint,
   credential, local user-home path, role holder or other fact about a real
   installation.

An unpopulated deployment record does not make the product baseline
incomplete. It shows that the public pack is ready to be instantiated without
pretending that a particular installation has been assessed.

## 3. Document dependencies and deliverables

Rows on the same step may be updated together. The state table in
[README](README.md) is the canonical roll-up and must agree with the document
headers.

| Step | Document | Reusable deliverable in this repository | Controlled-copy completion |
|---|---|---|---|
| 1 | [01 System overview and boundary](01-system-overview-and-boundary.md) | Product scope, components, stores, interfaces and configuration groups | Pin release/digest, installation scope, use case, classification, users and enabled components |
| 2 | [02 Data flows and trust boundaries](02-data-flows-and-trust-boundaries.md) | Product flows, boundaries, egress classes and enforced controls | Record actual endpoints, network placement, TLS and platform controls |
| 3 | [06 Identity and access](06-identity-and-access.md) | Authentication, session, role, governance and lifecycle controls | Record selected IdP, MFA, role holders, access processes and operator access |
| 3 | [07 Secrets and key management](07-secrets-and-key-management.md) | Secret classes, reference/resolution controls, key purposes and rotation effects | Record stores, custodians, readers, rotation and compromise actions |
| 3 | [08 Logging, audit and monitoring](08-logging-audit-and-monitoring.md) | Audit guarantees, retention mechanisms, emitted signals and response guidance | Record destinations, access, retention, alert rules, thresholds and responders |
| 4 | [04 Threat model](04-threat-model.md) | Assets, surfaces, STRIDE coverage, controls, severity and stable risk references | Calibrate likelihood, apply deployment decisions and populate controlled risks |
| 5 | [11 AI / LLM risk assessment](11-ai-llm-risk-assessment.md) | AI execution paths, responsible-AI questions, controls and limitations | Assess actual use cases, models, providers, people, impacts and approvals |
| 6 | [09 Secure development lifecycle](09-secure-development-lifecycle.md) | Change, review, test, CI and release controls | Re-measure mutable repository/release evidence for the assessed release |
| 6 | [10 Vulnerability and supply-chain management](10-vulnerability-and-supply-chain.md) | Intake, triage, remediation, dependency and release-integrity practices | Record deployment scanning, patch application and tighter local targets |
| 7 | [14 Security testing](14-security-testing.md) | Continuous product testing, internal review and findings workflow | Record independent test scope, provider, release, result and controlled report |
| 8 | [03 Shared responsibility matrix](03-shared-responsibility-matrix.md) | Stable allocation between product, maintainer, deployer, user and provider | Add installation-specific allocations only where the architecture requires them |
| 9 | [05 Statement of applicability](05-statement-of-applicability.md) | Complete import, reconciliation and row schema | Select the ISM release/baseline and assess every imported control |
| 9 | [12 Privacy impact assessment](12-privacy-impact-assessment.md) | Product data lifecycle, all-APP prompts, disclosure and assessment method | Assess actual purposes, people, data, legal basis, providers and privacy risks |
| 9 | [13 Incident response and continuity](13-incident-response-and-continuity.md) | Reusable response, evidence-preservation, recovery and exercise method | Assign contacts, notification rules, backups, RTO/RPO and exercise results |
| 9 | [15 Risk register](15-risk-register.md) | Risk schema, lifecycle, import and reconciliation rules | Populate sensitive risks, treatments, acceptance and review records |
| — | [16 Evidence index](16-evidence-index.md) | Product evidence catalogue and provenance | Add deployment configuration, scan, exercise and assessment evidence |

## 4. Rules for every document

- **State controls and limits precisely.** Say what ELSPETH enforces, where the
  control applies, and what remains a deployer or user responsibility. Avoid
  universal claims when Web, CLI, YAML or a particular exporter behaves
  differently.
- **Keep sensitive findings controlled.** Public documents may name threat
  classes, safe limitations and stable opaque risk IDs. Put reproduction
  detail, live exposure, likelihood, treatment and acceptance in the
  controlled risk register.
- **Separate product and deployment facts.** Every variable installation field
  belongs under **Deployment record** and uses `DEPLOYMENT-TODO:` until the
  controlled copy is populated.
- **Measure at a named commit.** Counts, defaults and inventories come from
  their live source of truth. Record the command, date, commit and relevant
  negative control. Remove volatile counts that do not help an assessor.
- **Trace claims to evidence.** Cite `[EV-nnn]` beside the claim it supports.
  Evidence records distinguish inspected implementation, automated tests, live
  configuration, procedures and executed operational records.
- **Use role owners in the reusable pack.** Name repository or product roles,
  not a person or a deployment operator. The controlled copy names its local
  owners and accepting authorities.
- **Apply corrections pack-wide.** When a control boundary changes, update all
  documents and evidence entries that describe it.

## 5. Validation before marking the product baseline reviewed

Run from the repository root with the checkout-local environment:

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=src:elspeth-lints/src \
.venv/bin/python -m pytest tests/unit/docs -n 0
```

The documentation suite includes the repository-wide transcript-marker hygiene
check, but it does not prove that every assurance-pack link or claim is valid.
Also perform and retain these direct checks:

1. Parse every Markdown file and verify each local file target and explicit
   section anchor. Control the checker with one known-valid and one known-broken
   link before trusting its result.
2. Confirm that every numeric evidence citation is defined once in
   [16](16-evidence-index.md), and that each evidence row's **Supports** field
   agrees with its claim-side citations.
3. Search for generic, temporary and unclassified placeholder forms; the
   result must be empty. Only the deployment marker defined in § 2 is valid.
4. Run `rg -n 'DEPLOYMENT-TODO:' docs/security-assurance/` and inspect every
   result. Each must be an installation field under a **Deployment record**
   heading.
5. Check that no real installation identifiers, credentials or absolute
   user-home paths appear.
6. Re-run every command whose result is quoted in a changed document and check
   its exit status directly.

Record the commands, raw exit codes and reviewed commit with the assessment or
release evidence. A passing documentation test suite is structural evidence;
it does not by itself prove the security claims.

## 6. Creating a deployment assessment pack

1. Copy this folder to the deployment's controlled repository.
2. Pin the assessed release: version, commit, image digest and supplied
   deployment bundle.
3. Complete the **Deployment record** in [01](01-system-overview-and-boundary.md)
   first; the other records inherit that boundary.
4. Replace every `DEPLOYMENT-TODO:` with an evidenced value or a justified
   *not applicable* decision.
5. Import the selected ISM control set into [05](05-statement-of-applicability.md)
   and reconcile source, imported and excluded counts.
6. Import all risk sources required by [15](15-risk-register.md), including
   residual and deployment-conditional threats, privacy and AI findings,
   control gaps, test findings and vulnerability exceptions.
7. Add deployment evidence to [16](16-evidence-index.md), including live
   configuration exports, scans, restore exercises, incident exercises and
   independent assessments.
8. Run § 5 against the controlled copy and retain the results.
9. Carry later public product corrections into the controlled copy while
   preserving the deployment's own evidence and decisions.

## 7. Pre-submission readiness checklist

The reviewed public product baseline has no open generic-documentation
blocker. The following work is required in the controlled deployment copy
before it is submitted for assessment. Treat an item as complete only when
the stated outcome is recorded and supported by evidence; a plan to obtain
the evidence is not a completed item.

| Gate | Required outcome before submission | Primary records |
|---|---|---|
| Freeze the assessment baseline | Record the product version, source commit, immutable image digest, deployment-bundle revision, assessment cut-off and change-control rule. Reconcile every document and test result to that baseline. | [01](01-system-overview-and-boundary.md), [09](09-secure-development-lifecycle.md), [16](16-evidence-index.md) |
| Complete the installation boundary | Define the use cases, owners, users, environments, data classification, enabled components, stores, interfaces, network paths, TLS termination, ingress, egress, providers, regions and contracts. | [01](01-system-overview-and-boundary.md), [02](02-data-flows-and-trust-boundaries.md), [03](03-shared-responsibility-matrix.md) |
| Close deployment placeholders | Replace every deployment-form `DEPLOYMENT-TODO:` with an evidenced value or a justified *not applicable* decision. Instructional examples in this lifecycle guide may retain the marker syntax. | All controlled-copy documents |
| Establish identity and access controls | Record the IdP and MFA policy, registration mode, session policy, role holders, approvers, administrators, joiner/mover/leaver process, access-review evidence, privileged operator access and any local-account use. | [06](06-identity-and-access.md) |
| Establish secret and key controls | Record every secret store, owner, reader, rotation schedule, compromise procedure, break-glass path, CI secret and signing-key custody arrangement. Exercise the material rotation and recovery paths. | [07](07-secrets-and-key-management.md), [13](13-incident-response-and-continuity.md) |
| Configure audit, monitoring and retention | Record log and telemetry destinations, access, alerts, thresholds, responders, time authority, payload-purge schedule, archive/export custody, legal holds and destruction evidence. | [08](08-logging-audit-and-monitoring.md), [12](12-privacy-impact-assessment.md) |
| Complete the Statement of Applicability | Import the selected authoritative ISM release, retain and hash the source, assess every control, reconcile all counts, complete the Essential Eight result and obtain assessor sign-off. | [05](05-statement-of-applicability.md) |
| Complete privacy and AI assessments | Assess the actual purposes, people, personal and sensitive information, models, providers, regions, subprocessors, disclosure, retention, human oversight, accessibility, explanation, contestability, legal basis and approval. | [11](11-ai-llm-risk-assessment.md), [12](12-privacy-impact-assessment.md) |
| Populate and reconcile risk | Import every residual and deployment-conditional threat, deficient or inherited control, privacy or AI finding, test finding, vulnerability exception, incident and architecture gap. Give each item a treatment or authorised, time-bounded acceptance and prove zero unresolved omissions. | [04](04-threat-model.md), [15](15-risk-register.md) |
| Refresh release and supply-chain evidence | Re-measure mutable repository settings; retain required-check results, dependency and image scans, SBOM and provenance, signature verification, deployment configuration and the exact release/digest tested. Resolve or accept every exception under policy. | [09](09-secure-development-lifecycle.md), [10](10-vulnerability-and-supply-chain.md), [16](16-evidence-index.md) |
| Complete independent security testing | Perform an appropriately scoped independent penetration test against the assessed release and architecture. Remediate and independently retest findings, or record authorised, time-bounded risk acceptance. | [14](14-security-testing.md), [15](15-risk-register.md) |
| Prove incident and continuity readiness | Assign contacts and authorities; record notification rules, RTO/RPO and backup coverage; run a restore exercise and an incident tabletop; retain results, deviations and follow-up actions as EV-804 and EV-805. | [13](13-incident-response-and-continuity.md), [16](16-evidence-index.md) |
| Assemble deployment evidence | Add configuration exports, IAM and network evidence, scan results, provider/inherited-control evidence, exercises, approvals and assessment reports from EV-901 upward. Record provenance, sensitivity, collector, date and reviewed release for every item. | [16](16-evidence-index.md) |
| Obtain final review and authorisation | Re-run § 5, reconcile the evidence, control and risk populations, obtain independent review where required, record submission approval and any operating conditions, expiry or review date, and freeze the submitted package. | [05](05-statement-of-applicability.md), [15](15-risk-register.md), [16](16-evidence-index.md) |

### 7.1 Known limitations requiring a treatment decision

These limitations are already disclosed in the product baseline. Each must be
fixed before submission when the assessment requires the missing capability,
or carried into the controlled risk register with an effective mitigation and
authorised, time-bounded acceptance:

1. **Audit-export authentication.** An enabled export requires an explicit
   signing decision, but may still be deliberately unsigned, and ELSPETH has
   no re-sign command for an existing bundle. For evidence relied upon as
   authentic, configure `authentication_policy: required`, use HMAC signing,
   retain every historical signer key for the evidence-retention period and
   exercise `elspeth audit-export verify` against the delivered artifact
   [EV-316].
2. **Retention automation.** Payload purge is an explicit operator action,
   audit metadata has no product-wide automatic expiry, and the pending-
   identity purge authority has no production caller. Define, operate and
   evidence the required purge, archive, legal-hold and deletion jobs; fix the
   missing automation where policy requires product enforcement [EV-325]
   [EV-326] [EV-812].
3. **Backup and whole-deployment recovery.** The supplied runbook does not
   schedule backups, prove that they ran, provide atomic cross-store recovery,
   or cover every store, secret and external effect. Build the deployment's
   complete backup design and pass a restore exercise against the assessed
   architecture [EV-007] [EV-804].
4. **Non-browser Web API identity.** The reviewed Web API has no implemented
   service credential. If the assessed use case requires machine access to the
   Web API, implement and assess that capability or redesign the integration;
   do not represent a human bearer token as a service identity. See
   [06 § 5.1](06-identity-and-access.md#51-inside-the-application).
5. **Independent testing.** The product repository contains no completed
   independent penetration-test record. Obtain one for the assessed release
   and deployment boundary, then close, retest or accept every finding under
   the rules in [14](14-security-testing.md).
6. **Operator-only trust-tier verification.** CI and agents do not hold the
   judge-metadata signing key. If the submission claims signed trust-tier
   allowlist clearance, the operator must run and retain the authoritative
   signature verification in the trusted context described by
   [07 § 5.1](07-secrets-and-key-management.md#51-judge-metadata-hmac-key).

A reviewer may identify additional treatments after the actual architecture,
data, providers and control baseline are known. Add those findings to the same
risk and evidence reconciliation rather than maintaining a separate informal
exceptions list.

### 7.2 Assessment-discovered engineering backlog

The assurance review also found the following product and repository-control
gaps. Correcting prose does not close these items. Each needs an implemented
change with regression evidence, or an explicit decision that the affected
capability is outside the submitted scope. Where the capability remains in
scope, an assessor or accepting authority may make the item release-blocking.

| Priority | Confirmed gap | Required engineering outcome | Submission effect |
|---|---|---|---|
| High | `identity_pending_retention_days` is validated, and a purge authority exists, but no production caller invokes it [EV-812]. | Add a supported scheduled or operator-invoked purge path with authority checks, audit evidence and lifecycle tests, or remove the setting and state that pending identities are retained until explicit administration. | Blocking when the submission claims that pending identities are automatically deleted after the configured period or when that deletion is required by the approved privacy schedule. |
| High | The optional gateway resolves ranged dependencies during each image build; it has no lockfile and is outside the root dependency audit and Dependabot coverage [EV-722]. | Add a reproducible lock/update path, dependency and image audit, SBOM/provenance coverage and CI enforcement for the gateway artifact. | Blocking when the gateway is part of the assessed deployment or supplied as an approved product artifact. Otherwise exclude it explicitly and require the deployer to build, scan and pin its own image. |
| High | Web authoring and run admission use active identity state rather than requiring the intended `user` role; `auditor` and `oversight` role grants add no route authority at this baseline [EV-104]. | Decide and enforce one coherent authorisation contract. If `user` is the author/run permission, add live route and mutation checks with negative tests. Keep reserved roles non-authorising until their permitted read surfaces and tests exist. | Blocking when the SoA or deployment design claims role-based least privilege for authoring/running, or claims implemented auditor/oversight access. Otherwise record the active-identity authority model and accept its residual risk. |
| Medium | Secret-reference enforcement, fingerprinting and export evidence cover managed references and recognised credential-bearing fields, not arbitrary text or unrecognised options [EV-208] [EV-215]. Universal sink-output redaction is not provided [EV-002]. | Either add a defined, tested data-loss-prevention boundary for the additional inputs and outputs in scope, or constrain authoring, provider and sink use so the field-scoped control is sufficient. Do not restore a universal “secrets never enter state or output” claim without corresponding enforcement. | Blocking only when the selected control baseline requires technical prevention across arbitrary user content or every sink. In all other cases retain `R-008`, user guidance and destination-specific controls. |
| Medium | The Web identity model reserves a `service` kind, but the Web API has no non-browser service credential and refuses service pre-provisioning [EV-104]. | If machine-to-Web-API use is required, implement a separately authenticated, least-privilege service identity with issuance, rotation, revocation, audit and route-authorisation tests. Otherwise remove machine access from the assessed use cases. | Blocking only for an architecture that requires unattended Web API access. Human bearer tokens must not be documented as service credentials. |
| Low | The Python audit command still carries a PyJWT advisory exception that matched no advisory for the locked version at the review date [EV-708]. | Remove the stale exception before the next public release, or retain it only with a fresh advisory match, reachability review, owner and expiry under the documented exception policy. Continue dated review of the Chroma client-only exceptions. | A stale unmatched exception is a release-hygiene defect; any matched exception follows the severity and acceptance policy in [10](10-vulnerability-and-supply-chain.md). |

The independent penetration test may add further software defects. Add each
validated finding to the engineering tracker and controlled risk register,
link the fixing commit and regression proof, and retain independent retest or
acceptance evidence. Do not treat this list as a substitute for that test.
