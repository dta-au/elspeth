# 13 — Incident response and continuity

**Status:** generic response and continuity method complete; deployment records
open · **Reviewed against:** `release/0.8.1` @ `eee4bb941` (2026-10-01) ·
**Owner:** ELSPETH maintainer

This document defines the reusable security-incident and continuity method for
ELSPETH. Each deploying organisation completes the labelled **Deployment
record** fields in its controlled copy. Those fields include people, contact
details, reporting obligations, infrastructure, backup services, recovery
objectives and exercise results; this public product document does not infer
them from a development machine.

The existing [incident-response runbook](../runbooks/incident-response.md) is
pipeline operations guidance for outages, data-quality problems and degraded
performance [EV-006]. It is useful input to technical diagnosis, but it is not
the security-incident procedure. In a suspected security incident, follow this
document first. Do not run its restart, deletion, cleanup, reprocessing or
database-maintenance steps until the incident commander and evidence custodian
have decided that the evidence and containment state permit them.

## 1. Purpose, scope and activation

Activate this method when an event might affect the confidentiality,
integrity, auditability or availability of ELSPETH, its data or a connected
service. Declare early when facts are uncertain; an incident may be downgraded
with a recorded reason after triage.

Covered triggers include:

- suspected unauthorised access, privilege use, account takeover or session
  misuse;
- disclosure, misrouting, over-sharing or incorrect classification of input,
  output, payload, transcript, audit or secret material;
- loss, suspected disclosure or unauthorised use of a credential, signing key,
  encryption key, fingerprint key or provider token;
- any Tier-1 audit-integrity violation, unexplained audit gap, failed audited
  read, audit-store corruption or evidence of direct audit-table modification;
- a malicious, substituted or compromised plugin, configuration, release
  image, dependency, model endpoint or external provider;
- destructive, corrupt or unauthorised writes to the Landscape, Sessions
  store, local credential store, payloads, managed blobs or outputs;
- an uncertain external sink effect, including a publish attempt whose exact
  application cannot be proved;
- sustained loss of service, failed dependency, failed deployment, lost
  coordination authority, unrecoverable run or suspected data loss; and
- an alert, user report, provider notice, vulnerability disclosure or
  regulatory enquiry that indicates one of the conditions above.

This method covers declaration, containment, evidence, investigation,
correction, recovery, notification and review. Provider-side forensics,
host/network containment and legal reporting use the deploying organisation's
approved procedures, with their decisions recorded here.

### 1.1 Deployment record — declaration

Open one controlled incident record and assign a unique incident identifier.
At declaration, record the following even when the value is only “unknown”:

| Field | Incident record value |
|---|---|
| Incident identifier and controlled record location | DEPLOYMENT-TODO: |
| Detection time, declaration time and time zone | DEPLOYMENT-TODO: |
| Reporter and detection source | DEPLOYMENT-TODO: |
| Initial symptoms and affected service or workflow | DEPLOYMENT-TODO: |
| Suspected confidentiality, integrity, auditability and availability impact | DEPLOYMENT-TODO: |
| Provisional severity and rationale | DEPLOYMENT-TODO: |
| Known run, session, request, user, release and provider identifiers | DEPLOYMENT-TODO: controlled copy only |
| Incident commander and next update time | DEPLOYMENT-TODO: |

## 2. Roles, authority and communications

One person may hold more than one role in a small response, but each
responsibility must have a named holder and alternate in the controlled copy.

| Functional role | Responsibility |
|---|---|
| Incident commander | Declares and classifies the incident; sets objectives; authorises containment, recovery and closure; resolves conflicts between response streams. |
| Technical lead | Diagnoses the affected ELSPETH, application, data and platform components; proposes containment and recovery actions; records commands and results. |
| Evidence custodian | Defines the preservation scope; captures, hashes, labels, stores and transfers evidence; maintains the custody log; approves actions that may alter evidence. |
| System owner | Decides service priorities, accepted degradation, business workarounds and return to service within delegated authority. |
| Privacy/legal adviser | Determines affected information, jurisdiction, contractual and statutory obligations, privilege and notification requirements. |
| Communications lead | Controls internal, customer, provider, regulator and public messages; records what was sent, when and under whose authority. |
| Platform or service owner | Contains and restores the hosting, database, identity, secret, logging, network or provider service under its own approved runbooks. |

### 2.1 Deployment record — roster and authorities

| Field | Deployment value |
|---|---|
| Incident commander, alternate and contact method | DEPLOYMENT-TODO: controlled copy only |
| Technical lead, alternate and contact method | DEPLOYMENT-TODO: controlled copy only |
| Evidence custodian, alternate and approved evidence repository | DEPLOYMENT-TODO: controlled copy only |
| System owner and delegated outage/data-loss authority | DEPLOYMENT-TODO: controlled copy only |
| Privacy/legal adviser and after-hours contact | DEPLOYMENT-TODO: controlled copy only |
| Communications lead and approval authority | DEPLOYMENT-TODO: controlled copy only |
| Platform, database, identity, network, secret-store and provider escalation contacts | DEPLOYMENT-TODO: controlled copy only |
| Incident channel, conference bridge and fallback channel | DEPLOYMENT-TODO: controlled copy only |
| Controlled incident-record location and access list | DEPLOYMENT-TODO: controlled copy only |
| Authority to disable traffic, stop pipelines, revoke credentials, isolate hosts, restore data and accept data loss | DEPLOYMENT-TODO: controlled copy only |

Use the incident identifier in every response record. Keep row contents,
transcripts, payloads, credentials and personal information out of broad chat
channels and tickets. Put sensitive evidence in the controlled evidence
repository and link it by identifier.

## 3. Severity classification

Classify the highest credible impact across all four dimensions. Reassess after
each material finding and record every change.

| Severity | Confidentiality | Integrity and auditability | Availability and recovery |
|---|---|---|---|
| **Critical** | Confirmed or credible large-scale disclosure of sensitive data, privileged access or high-impact credential/key compromise | Landscape or other authoritative evidence cannot be trusted; unauthorised production change; unsafe external effect may be repeated | Critical service unavailable with no safe workaround, destructive loss, or recovery cannot meet an approved maximum tolerance |
| **High** | Confirmed limited disclosure or likely unauthorised access to protected data | Material output, routing, identity, policy or audit record may be wrong, incomplete or unauthorised, but scope can be bounded | Major service or workflow unavailable, recovery uncertain, or significant backlog/data loss is possible |
| **Moderate** | Exposure is contained to low-impact data or prevented before disclosure | Control failure or incorrect result has bounded impact and authoritative evidence remains usable | Degraded service with a safe workaround and no evidence of material data loss |
| **Low** | No protected data exposure | No unauthorised or material change; control operated as designed or issue is cosmetic | Negligible service impact and routine correction is safe |

An increment of a counter classified **Critical** in the Tier-1 runbook starts
as Critical; an audit-access failure classified **High** starts as High. Raise
either classification when the authoritative record may be untrustworthy. A
credential event is classified by the credential's reachable authority and
exposure window, not by whether misuse has already been observed. The
deploying organisation maps these criteria to its local labels and response
targets.

### 3.1 Deployment record — severity and escalation

| Product severity | Local label | Acknowledge target | Incident-command target | Update interval | Escalation path |
|---|---|---|---|---|---|
| Critical | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| High | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Moderate | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Low | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |

## 4. Response procedure

Notification and evidence work can run in parallel with technical containment;
the phase numbers express control dependencies, not permission to delay a
deadline.

### Phase 1 — detect and declare

1. Record the original alert or report without rewriting it.
2. Open the controlled record, timestamp the declaration and appoint the
   incident commander.
3. Classify the highest credible impact and affected trust boundaries.
4. Start the escalation and update schedule defined by the deployment.

**Exit criterion:** the incident has an identifier, commander, provisional
severity, affected scope and next update time.

### Phase 2 — stabilise and contain

1. Stop the suspected harmful path at the narrowest safe boundary: pause an
   affected run, block an endpoint or egress path, disable an account, revoke a
   credential, quarantine a release or drain a service as the facts require.
2. Preserve access for authorised evidence collection. Prefer reversible,
   logged controls over deletion or in-place repair.
3. Separate unaffected services only after confirming that shared databases,
   identities, secrets, providers and outputs do not carry the same exposure.
4. Record the action, actor, authority, time, target, expected effect and
   verification result.

**Exit criterion:** continuing harm is stopped or bounded, or the commander
has explicitly accepted and recorded why a containment action is unsafe.

### Phase 3 — preserve evidence

1. Capture the sources in § 5 before repair, restart, retention purge, restore,
   credential rotation or reprocessing changes them.
2. Record acquisition method, source, time, hash or provider snapshot
   identifier, custodian, storage location and every transfer.
3. Work from copies where practicable. Keep originals read-only and retain the
   failed state until the evidence custodian releases it.
4. Do not patch Tier-1 audit rows, synthesize missing transcript rows, change
   sink-effect status with SQL or repeat an uncertain publish operation.

**Exit criterion:** the custodian confirms that volatile and durable evidence
needed to establish scope and timeline has been captured or that a documented
containment priority required an exception.

### Phase 4 — investigate and assess scope

1. Build a timeline from authoritative records and provider/platform evidence.
2. Identify the initial access or failure, affected identities, data, runs,
   sessions, components, external calls and outputs.
3. Determine whether the same condition exists in other releases,
   environments, tenants, workflows or providers.
4. Track facts, hypotheses and unknowns separately. Record the query or tool
   that supports each conclusion.
5. Start and continually update the reporting decision in § 7.

**Exit criterion:** the commander can state the known scope, remaining
unknowns, likely cause and containment confidence.

### Phase 5 — eradicate or correct

1. Remove malicious access and persistence; revoke affected credentials and
   sessions through their owning systems.
2. Correct the vulnerability, configuration, policy, dependency, data or
   platform fault through the controlled change process.
3. Follow [07 — Secrets and key management](07-secrets-and-key-management.md)
   for key-specific recovery effects. Rotation can invalidate sessions,
   secret ciphertext, fingerprints, links or signatures and must not be
   treated as a value-only replacement [EV-202] [EV-203] [EV-226].
4. Use the emergency security-fix path in
   [10 — Vulnerability and supply-chain management](10-vulnerability-and-supply-chain.md)
   when a product correction is required; record approval, build provenance,
   deployment identity and rollback decision.

**Exit criterion:** the cause and persistence mechanisms are removed or
explicitly accepted, the corrected state is identified immutably, and recovery
preconditions are met.

### Phase 6 — recover and validate

1. Follow the recovery order and stop conditions in § 10.
2. Prove database, storage, schema, configuration, secret and release
   compatibility before enabling writers.
3. Validate representative identity, session, run, audit and output paths.
4. Use checkpoint resume or web handoff only within the boundaries in § 9.

**Exit criterion:** the system owner and incident commander approve return to
service against recorded validation evidence and accepted residual risk.

### Phase 7 — notify and communicate

1. Make each notification decision against the deployment's jurisdiction,
   contracts, classification, affected people and current facts.
2. Have the authorised approver review the message and recipient list.
3. Record the deadline source, decision time, content version, transmission
   evidence and follow-up commitment.
4. Correct earlier communications when material facts change.

**Exit criterion:** every applicable obligation is met or a documented,
authorised decision explains why it is not applicable.

### Phase 8 — close and review

1. Confirm containment remains effective and heightened monitoring has
   completed for the deployment-defined period.
2. Record cause, impact, data loss, downtime, decisions, recovery point and
   residual risk.
3. Assign each corrective action an owner, due date, verification method and
   risk reference.
4. Update runbooks, controls, alerts, tests and training affected by the
   incident.
5. Close only when the system owner, evidence custodian and incident commander
   have signed the record and notification follow-ups are tracked.

### 4.1 First-hour checklist

Run these steps in order unless immediate containment of active harm takes
priority. Record any reordering and why.

1. Capture the alert, current time, reporter and original observable signal.
2. Declare the incident, assign the incident commander and open the controlled
   record.
3. Assign technical lead and evidence custodian; notify the system owner and
   privacy/legal adviser.
4. Classify the highest credible impact and set the next update time.
5. Identify affected and potentially shared identities, stores, services,
   providers, releases, runs, sessions and outputs.
6. Apply the narrowest safe containment and verify its effect without deleting
   data or rewriting the failed state.
7. Capture volatile evidence and snapshot durable evidence according to § 5.
8. Check product and deployment signals in § 6 and record exact timestamps,
   request IDs, run IDs and provider event IDs.
9. Block uncertain sink effects and stop manual or automated retries until
   reconciliation proves whether the exact effect was applied.
10. Begin the reporting decision record, including the affected data and the
    source of every possible deadline.
11. Record current user/business impact, safe workarounds and any accepted
    service degradation.
12. Set the next technical, reporting and stakeholder actions with named
    owners and times.

**Stop and escalate** before deleting or cleaning files, restarting a source
that may destroy volatile evidence, restoring over the failed state, manually
editing audit or coordination rows, re-keying encrypted data, reprocessing
source data, repeating an external sink operation, or returning traffic to a
system whose integrity or release identity is unproved.

## 5. Evidence preservation and custody

Preserve only the sources relevant to the incident, but make the scope
decision explicit. A normal set is:

| Source | Preserve before change | Handling notes |
|---|---|---|
| Landscape audit database | Database or provider snapshot, relevant journals/exports, connection target and integrity result | It is the authoritative run record. Never coerce Tier-1 rows to make a read or verification pass. |
| Sessions database | Database or provider snapshot and schema/release identity | Holds session, identity, Composer, governance and session-operation coordination state. |
| Local `auth.db`, when local authentication is enabled | Owner-only copy or platform snapshot | It is a separate credential store; restrict access as credential material. |
| Payload store | Referenced content, hashes, retention state and storage metadata | A hash without content does not permit later rehydration. Preserve referenced payloads before purge. |
| Managed blob directory | Affected session subtrees, file metadata and content hashes | Treat uploaded and generated content according to its classification. |
| Pipeline outputs and external sinks | Output objects, target audit/version history and sink-effect records | Preserve target-side evidence before correction; do not manually repeat an uncertain effect. |
| Configuration and release identity | Effective redacted configuration, settings file, commit, package/image digest, plugin inventory and deployment history | Store secret references, not plaintext secret values, in the general incident record. |
| Secrets and keys | Secret version identifiers, access/revocation logs and custody record | Export key material only when the deployment's approved recovery and evidence policy requires it. |
| Logs, metrics and traces | Bounded incident window plus clock/time-zone information | Operational telemetry is diagnostic evidence; it does not replace Landscape audit evidence. |
| LLM, identity, cloud and other provider records | Request/event identifiers, provider audit logs, policy/configuration snapshot and support correspondence | Capture through the provider's approved export or preservation mechanism. |
| Host, container and network state | Process/container state, image digest, orchestration events, network/security-control logs and relevant volatile state | Use platform forensic procedures; do not place this device's identifiers in the public pack. |

For a Tier-1 incident, start with the preservation and triage procedure in
[Audit Tier-1 violation](../runbooks/audit-tier1-violation.md) [EV-308]
[EV-311]. It tells operators to stop retries, capture the deployment version and
request/session identifiers, avoid copying payloads into incident channels and
preserve the Sessions database before repair.

### 5.1 Deployment record — evidence custody

For each item, record:

| Field | Evidence record value |
|---|---|
| Incident and evidence-item identifiers | DEPLOYMENT-TODO: |
| Source system, account/subscription and exact object | DEPLOYMENT-TODO: controlled copy only |
| Acquisition date/time, time zone, actor and authority | DEPLOYMENT-TODO: |
| Acquisition command, query, export or snapshot method | DEPLOYMENT-TODO: controlled copy only |
| Hash, immutable version or provider snapshot identifier | DEPLOYMENT-TODO: |
| Original and working-copy locations | DEPLOYMENT-TODO: controlled copy only |
| Classification, access list, encryption and retention | DEPLOYMENT-TODO: controlled copy only |
| Each transfer, access or transformation | DEPLOYMENT-TODO: |
| Disposal or release authority and date | DEPLOYMENT-TODO: |

## 6. Product signals and runbook routing

The product signal inventory and limitations are maintained in
[08 § 5.1](08-logging-audit-and-monitoring.md#51-signals-the-product-provides)
[EV-328] [EV-329]. Deployment thresholds and recipients remain local.

| Signal or condition | First product-specific action | Continue with |
|---|---|---|
| `/api/health` fails | Treat the process as unable to answer HTTP; capture platform and process evidence | Deployment platform runbook, then the pipeline operations [incident-response runbook](../runbooks/incident-response.md) if security compromise is not suspected |
| `/api/ready` returns 503 or `readiness_check_not_ready` appears | Record the named redacted check and its bounded detail; determine whether the dependency failure is malicious, corrupt or operational | This method for security/integrity suspicion; otherwise the applicable [AWS ECS](../runbooks/aws-ecs-deployment.md) or [Azure Container Apps](../runbooks/azure-container-apps-deployment.md) runbook |
| Composer audit-integrity counter increments or an audit-grade read fails | Stop retries and preserve the Sessions store | [Audit Tier-1 violation](../runbooks/audit-tier1-violation.md) |
| Authentication suppression, rejected share token or unrecognised administrative event | Preserve `auth_events`, request identifiers and the relevant Sessions/local-auth evidence; contain the identity or token | [06 — Identity and access](06-identity-and-access.md) and this method |
| Audited run or external-call failure | Diagnose from Landscape or read-only analysis tools; do not assume resume is safe | Pipeline operations [incident-response runbook](../runbooks/incident-response.md), then [Resume failed run](../runbooks/resume-failed-run.md) only if eligible |
| Stuck lease, expired run leader or coordination anomaly | Preserve coordination rows and identify the current authority before action | [Scheduler lease recovery](../runbooks/scheduler-lease-recovery.md) |
| Prepared, uncertain or blocked external sink effect | Stop retries; inspect the durable effect and target-side evidence | [Sink-effect recovery](../runbooks/sink-effect-recovery.md) |
| Suspected loss or corruption requiring backup | Stop writers and preserve the failed state before restore | [Backup and recovery](../runbooks/backup-and-recovery.md) within the boundaries in § 8 |
| Credential, signing-key or secret compromise | Bound the authority and exposure window; preserve access/use logs before revocation where safe | [07 § 6](07-secrets-and-key-management.md#6-rotation-and-compromise) and provider/secret-store procedure |
| Vulnerable or compromised release/dependency/plugin | Quarantine the affected identity and preserve build/deployment evidence | [10 — Vulnerability and supply-chain management](10-vulnerability-and-supply-chain.md) |

## 7. Reporting and notification decision

The product cannot determine jurisdiction, agency policy, data-breach
applicability, contractual notice, classification handling or deadlines. The
privacy/legal adviser makes those decisions from the incident facts and the
deployment's approved obligation register. Possible authorities must never be
treated as automatically applicable or as an exhaustive list.

Make and retain a decision even when the outcome is “notification not
required.” Reassess whenever scope, affected data, affected people or impact
changes.

### 7.1 Australian Notifiable Data Breaches decision path — when applicable

Use this path only where the deployment's obligation register establishes that
the entity and affected information are subject to the Australian NDB scheme.
Other Australian, state, territory, contractual and overseas duties remain
separate. This procedure follows the OAIC's
[quick reference guide for responding to data breaches](https://www.oaic.gov.au/privacy/notifiable-data-breaches/quick-reference-guide-for-responding-to-data-breaches)
and detailed
[NDB scheme guidance](https://www.oaic.gov.au/privacy/notifiable-data-breaches/preventing-preparing-for-and-responding-to-data-breaches/data-breach-preparation-and-response/part-4-notifiable-data-breach-ndb-scheme)
[EV-814].

1. **Trigger and clock.** When the entity becomes aware of reasonable grounds
   to suspect that there may have been an eligible data breach, record the
   grounds and awareness date/time, assign the assessment owner, and promptly
   start a reasonable and expeditious assessment. Do not wait for certainty,
   completed forensics, a board meeting or a later incident phase.
2. **Assessment target.** Take all reasonable steps to complete the assessment
   within 30 calendar days after the day of awareness. Treat 30 days as the
   maximum wherever possible and aim to finish sooner. If completion in that
   period is not reasonable, record the steps taken, reason for delay,
   remaining questions, owners and dated completion plan; continue without
   pause.
3. **Eligibility test.** Record a supported yes/no/unknown answer for each
   element: (a) personal information held by the entity was subject to
   unauthorised access or disclosure, or was lost in circumstances likely to
   result in unauthorised access or disclosure; (b) a reasonable person in the
   entity's position would regard serious harm to one or more individuals as
   likely—more probable than not—after a holistic assessment; and (c) remedial
   action has not prevented that likely risk. Consider the information and
   safeguards, whether safeguards can be overcome, possible recipients and
   their intent/capability, affected cohorts, and serious physical,
   psychological, emotional, financial or reputational harm.
4. **Remedial-action branch.** Contain and remediate at any time, then
   re-evaluate likely serious harm. If action prevents the likely risk for all
   individuals—or, for lost information, prevents unauthorised access or
   disclosure—record why the incident is not eligible for NDB notification
   and continue other reporting, review and corrective action. If only some
   people are protected, continue the eligibility and notification path for
   the remainder.
5. **Transition without delay.** As soon as there are reasonable grounds to
   believe an eligible breach occurred, whether before, during or at the end
   of assessment, move immediately to notification; do not wait for the
   30-day target. Record any statutory exception and whether it leaves a
   partial duty. Unless an exception applies, as soon as practicable give the
   OAIC a statement and notify the individuals at risk of serious harm. The
   statement identifies the entity/contact, describes the breach and kinds of
   information, and recommends practical steps. If neither direct-notification
   option is practicable, publish the statement and take reasonable steps to
   publicise its contents.

### 7.2 Deployment record — obligations, assessment and decision

| Field | Deployment or incident value |
|---|---|
| Governing jurisdictions, agency policy, contracts and classification rules | DEPLOYMENT-TODO: controlled copy only |
| Potential authorities, customers, data subjects, providers and internal offices | DEPLOYMENT-TODO: controlled copy only |
| Approved contact route for each recipient | DEPLOYMENT-TODO: controlled copy only |
| Deadline or timing rule and its authoritative source | DEPLOYMENT-TODO: controlled copy only |
| Decision owner and delegated authority | DEPLOYMENT-TODO: controlled copy only |
| Decision time and facts available at that time | DEPLOYMENT-TODO: |
| Affected data, classification, people, services and jurisdictions | DEPLOYMENT-TODO: controlled copy only |
| Applicability decision and rationale for each obligation | DEPLOYMENT-TODO: controlled copy only |
| NDB suspicion grounds, awareness date/time, assessment owner and 30-calendar-day target | DEPLOYMENT-TODO: controlled copy only |
| Assessment start/status/completion, reasonable steps and any delay rationale | DEPLOYMENT-TODO: controlled copy only |
| Eligibility limbs, facts, affected cohorts and serious-harm rationale | DEPLOYMENT-TODO: controlled copy only |
| Remedial action, verification and cohort-specific effect on likely harm | DEPLOYMENT-TODO: controlled copy only |
| Reasonable-grounds-to-believe time, exception decision and notification population | DEPLOYMENT-TODO: controlled copy only |
| OAIC statement fields/version/time and individual notification or publication evidence | DEPLOYMENT-TODO: controlled copy only |
| Notification approver, content version and legal/communications review | DEPLOYMENT-TODO: controlled copy only |
| Recipient, transmission time and delivery evidence | DEPLOYMENT-TODO: controlled copy only |
| Follow-up, correction or continuing-update commitment | DEPLOYMENT-TODO: controlled copy only |

## 8. Backup responsibilities and coverage

ELSPETH does not schedule backups, provide a backup repository, coordinate an
atomic cross-store snapshot, select retention, monitor backup jobs, restore a
whole deployment or promise a recovery time objective (RTO) or recovery point
objective (RPO). The deploying organisation provides and monitors those
controls.

The product [backup-and-recovery runbook](../runbooks/backup-and-recovery.md)
supplies examples for a SQLite online backup of the Landscape and Sessions
databases, a payload archive, a settings-file copy, a logical PostgreSQL dump
of one configured database URL, SQLite Landscape/payload restoration and
basic SQLite/Landscape verification [EV-007]. It does not prove that any
backup ran or restored successfully. It does not provide a local `auth.db` or
managed-blob procedure, a PostgreSQL restore procedure, secret/key recovery,
an atomic snapshot across stores, or full external-output recovery. The
runbook's payload-retention default is not a backup-retention commitment.

### 8.1 Backup inventory and ownership

| State | Product boundary | Deployment responsibility |
|---|---|---|
| Landscape audit database | Product records and can inspect run/audit state; the runbook gives limited SQLite backup/restore and single-URL PostgreSQL dump examples | Back up the actual database, logs and provider metadata; encrypt, monitor and restore it; coordinate its point with dependent stores |
| Sessions database | Product owns its schema and web state; the runbook gives a SQLite backup example | Back up and restore the actual SQLite or PostgreSQL store and prove schema/release compatibility |
| Local `auth.db` | Product creates a separate owner-only SQLite credential store when local authentication is used | Include it in a restricted backup and restore design, or record an approved rebuild/re-provision decision |
| Payload store | Product records hashes/references and the runbook gives filesystem archive/extract examples | Preserve referenced content and metadata with the Landscape recovery point; protect and test the archive or provider backup |
| Managed blobs | Product uses a separate managed blob directory for web uploads and generated content | Back up and restore it with the Sessions records that reference it; prove file/hash/ownership consistency |
| Configuration and release artefacts | Product can be reconstructed from versioned source/images and settings; the runbook gives a settings-file copy example | Retain approved settings, plugin inventory, exact image/package digest and deployment definition |
| Secrets and recovery key material | Product consumes secret references and keys; some key changes intentionally break old sessions, ciphertext, fingerprints, links or signatures | Provide secret-manager versioning, backup/escrow where approved, revocation and recovery procedures; prove the restored service can decrypt and verify required records |
| Pipeline outputs and external effects | Product records output lineage and durable sink-effect state for supported sinks | Back up or reconstruct target data according to business policy and reconcile target-side state before retry |
| Logs, metrics, identity and provider evidence | Product emits bounded signals and audit identifiers | Retain provider/platform records for the required investigation and recovery window |

### 8.2 Deployment record — backup control

Complete one row per distinct store or service. Separate configuration,
secret references and key material when they have different custodians or
recovery effects.

| Field | Deployment value |
|---|---|
| Store/service and authoritative purpose | DEPLOYMENT-TODO: repeat per item |
| Platform, account/subscription, region and resource identifier | DEPLOYMENT-TODO: controlled copy only |
| Backup mechanism, destination and format | DEPLOYMENT-TODO: controlled copy only |
| Cross-store consistency or quiescing method | DEPLOYMENT-TODO: |
| Schedule/frequency and event-triggered backups | DEPLOYMENT-TODO: |
| Retention, immutability and deletion authority | DEPLOYMENT-TODO: |
| Encryption and key/secret recovery dependency | DEPLOYMENT-TODO: controlled copy only |
| Backup writer, reader and restore principals | DEPLOYMENT-TODO: controlled copy only |
| Monitoring, alert, last successful job and unresolved failures | DEPLOYMENT-TODO: controlled copy only |
| Restore procedure and platform runbook | DEPLOYMENT-TODO: controlled copy only |
| Last isolated restore exercise and evidence reference | DEPLOYMENT-TODO: controlled copy only |

## 9. Resume and automatic handoff boundaries

Resume and handoff continue durable work after process loss. They are not
backups, multi-store restoration or unconditional disaster recovery.

### 9.1 CLI checkpoint resume

`elspeth resume` checks the run state, checkpoint availability and compatibility
before execution. The implementation can refuse a missing or terminal run, a
run with a live leader, missing or incompatible checkpoint state, incomplete
source lifecycle or an unsatisfiable bound group. It can admit a failed or
interrupted run, and a running run whose leader seat is absent or expired,
only when the remaining gates pass. Resume re-drives persisted scheduler work
through a null source; it does not reopen the original source to discover
unread rows [EV-801].

Resume therefore requires the correct compatible release, settings and graph;
an intact Landscape and referenced payload state; a usable checkpoint;
recoverable durable work; and safe reconciliation of any sink effect. Run the
documented dry run before `--execute`. Honour a refusal: preserve the run and
either start a fresh run or use the documented abandonment path. Never change
audit rows to make a run appear resumable.

### 9.2 Web durable handoff and terminal reconciliation

The web recovery coordinator searches for recoverable session run records. It
defers to a live in-process executor or Landscape leader, acquires the Sessions
operation authority, rebinds ownership, then either transfers work to an
executor or projects authoritative terminal Landscape status back into
Sessions. It marks recovery as required rather than starting when a required
execution baseline is missing. Fencing and compare-and-swap authority can
refuse a stale recoverer [EV-802].

This mechanism depends on mutually usable Sessions, Landscape, blob/payload
and execution-input state. It does not reconstruct a lost database, restore a
blob directory, recover secret material or establish a consistent cross-store
recovery point.

### 9.3 External sink effects

Supported durable sink effects reconcile the exact prepared plan before
another commit. If reconciliation returns `UNKNOWN`, the coordinator stops and
the effect and successors remain blocked. `UNKNOWN` is an operator boundary,
not permission to retry, manually publish or set a database status [EV-803].
Follow [Sink-effect recovery](../runbooks/sink-effect-recovery.md) and retain
target-side evidence with the Landscape record.

## 10. Recovery order and validation

Use the deployment's platform commands; this document fixes the safe order and
evidence requirements.

1. **Control authority and writers.** Confirm incident command, stop or drain
   writers, block automated retries and record any external system that may
   still write.
2. **Preserve the failed state.** Snapshot or copy affected databases,
   filesystems, provider logs and target state before overwriting or repairing
   anything.
3. **Choose the recovery mode.** Record whether the action is clean rebuild,
   restore, product resume, web handoff or business repair. Do not use resume
   to compensate for missing backups.
4. **Select a consistent recovery point.** Reconcile Landscape, Sessions,
   local credentials, payloads, managed blobs, outputs and provider state.
   Record known skew, data loss and the authority accepting it.
5. **Restore the trusted release and configuration.** Verify the source,
   package or image digest and its compatibility with the restored schemas,
   settings, graph and plugins.
6. **Restore data in dependency order.** Restore the Landscape and Sessions
   stores chosen for the same recovery decision, then local credentials when
   used, payloads and managed blobs, configuration, and required outputs.
   Restore secret/key material through the deployment's secret manager; do not
   copy plaintext secrets into the incident record.
7. **Run provider and data-integrity checks.** Use database/provider-native
   integrity checks, verify schema/release compatibility, confirm referenced
   payloads and blobs, and inspect the restored Landscape. Record commands,
   versions, outputs and exit status.
8. **Start without user traffic.** Verify process liveness and the complete
   readiness report, including database, schema, data-directory, payload,
   blob and instance-membership checks [EV-328].
9. **Validate representative behaviour.** Authenticate through the deployed
   mode; open a representative session; inspect/explain a representative run;
   verify audit access, payload/blob retrieval and an approved output path.
10. **Dry-run continuity actions.** If continuing an interrupted run, run the
    resume eligibility check without execution. Resolve or accept every
    refusal. Reconcile prepared or uncertain sink effects before execution.
11. **Authorise re-entry.** The technical lead presents results and remaining
    gaps; the system owner and incident commander approve writers and traffic.
12. **Monitor and reconcile.** Watch the deployment-defined heightened signals,
    compare restored counts/records to the accepted recovery point and reopen
    the incident on recurrence or unexplained divergence.

Stop recovery if stores cannot be tied to one accepted recovery decision,
integrity or schema checks fail, required key material is unavailable, the
release identity is unproved, readiness fails, resume refuses, or a sink effect
remains unexplained. Record and escalate the condition; do not bypass it.

## 11. Recovery objectives and deployment topology

RTO is the approved maximum time from the deployment-defined start event to
validated restoration of the stated service. RPO is the approved maximum
amount of committed state that may be lost, expressed as a time or an exact
recovery-point rule. ELSPETH sets neither value.

### 11.1 Deployment record — environment and dependencies

| Field | Deployment value |
|---|---|
| Assessed release, commit and immutable image/package digest | DEPLOYMENT-TODO: |
| Hosting platform, account/subscription, region and environment identifier | DEPLOYMENT-TODO: controlled copy only |
| Server, device, node, task, container-app or workload identifiers | DEPLOYMENT-TODO: controlled copy only |
| Landscape and Sessions database engines, instances and endpoints | DEPLOYMENT-TODO: controlled copy only |
| Local-auth store location and host/device dependency, or not used | DEPLOYMENT-TODO: controlled copy only |
| Payload and managed-blob storage resources | DEPLOYMENT-TODO: controlled copy only |
| Secret stores, recovery-key dependencies and restore principals | DEPLOYMENT-TODO: controlled copy only |
| Network, DNS, certificate, identity-provider and external-provider dependencies | DEPLOYMENT-TODO: controlled copy only |
| Pipeline output/sink systems and reconciliation owners | DEPLOYMENT-TODO: controlled copy only |
| Platform recovery runbooks and access prerequisites | DEPLOYMENT-TODO: controlled copy only |

### 11.2 Deployment record — RTO, RPO and recovery order

Complete one row per service or store. A combined value is acceptable only
when the same recovery point, owner and validation evidence apply.

| Service/store | Priority and dependencies | RTO and start/end definition | RPO and recovery-point rule | Accepted degradation or data loss authority | Validation and last measured result |
|---|---|---|---|---|---|
| Landscape audit database | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Sessions database | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Local `auth.db`, if used | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Payload store | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Managed blobs | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Web service / API | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Each critical pipeline and output/sink | DEPLOYMENT-TODO: repeat as needed | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |

## 12. Exercises and assurance

A written plan proves preparedness only. A deployment-held tabletop, restore
exercise or real incident record provides evidence that its people, access,
backups and platform procedures work [EV-804] [EV-805].

### 12.1 Tabletop method

1. Select a scenario that exercises at least two trust boundaries. Rotate
   among credential compromise, Tier-1 audit failure, provider compromise,
   data disclosure, malicious configuration/plugin, uncertain sink effect and
   destructive loss.
2. Define objectives, starting facts, injects and observers. Do not disclose
   later injects to responders.
3. Include the incident commander, technical lead, evidence custodian, system
   owner, privacy/legal adviser, communications lead and relevant platform or
   provider owner.
4. Walk declaration, severity, first-hour actions, evidence custody,
   containment, reporting decisions, continuity boundaries, recovery approval
   and closure.
5. Capture timestamps, decisions, artefacts requested, missing access,
   ambiguous steps and unsafe assumptions.
6. Score the success criteria below, create corrective actions and retest
   failed criteria by their due dates.

Tabletop success requires: all functional roles can be reached; the event is
declared and classified with reasons; containment preserves evidence;
sensitive content stays in controlled channels; reporting applicability and
deadline sources are recorded; resume, restore and sink-effect boundaries are
applied correctly; recovery has explicit validation and approval; and every
gap has an owner and due date.

### 12.2 Restore-exercise method

1. Use an isolated environment that cannot write to production systems or
   external sinks.
2. Select backups using the normal inventory and retention process. Record the
   chosen recovery point before inspecting which copy is easiest to restore.
3. Start the RTO clock at the deployment-defined event and record every wait,
   missing permission and manual dependency.
4. Restore all state required by the scenario, including secret/key material
   through its approved recovery path.
5. Run database/provider integrity, schema/release compatibility, readiness,
   representative session/run, audit, payload/blob and output validation from
   § 10.
6. Measure the achieved recovery point against the RPO and the validated
   return-to-service time against the RTO. Record data that could not be
   recovered or verified.
7. Destroy or reclassify restored sensitive copies under the exercise plan,
   retain the evidence record, assign corrective actions and repeat any failed
   element.

### 12.3 Deployment record — exercise and incident results

| Field | Deployment value |
|---|---|
| Exercise/incident identifier, type, scenario and date | DEPLOYMENT-TODO: controlled copy only |
| Scope, release, platform and recovery point tested | DEPLOYMENT-TODO: controlled copy only |
| Participants, observers and absent required roles | DEPLOYMENT-TODO: controlled copy only |
| Start, declaration, containment, restore and validation times | DEPLOYMENT-TODO: |
| Reporting decisions and communications exercised | DEPLOYMENT-TODO: controlled copy only |
| Backups selected and cross-store consistency result | DEPLOYMENT-TODO: controlled copy only |
| Integrity, readiness, representative workflow and sink checks | DEPLOYMENT-TODO: controlled copy only |
| Measured RTO, RPO and data loss against approved objectives | DEPLOYMENT-TODO: controlled copy only |
| Success criteria passed/failed and evidence location | DEPLOYMENT-TODO: controlled copy only |
| Corrective action, owner, due date, status and retest evidence | DEPLOYMENT-TODO: controlled copy only |
| Approval and next exercise date/trigger | DEPLOYMENT-TODO: controlled copy only |

### 12.4 Deployment record — exercise cadence

| Activity | Required frequency or trigger | Owner | Last completed | Next due | Evidence |
|---|---|---|---|---|---|
| Incident-response tabletop | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Full cross-store restore | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Credential/key compromise drill | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Sink-effect reconciliation drill, if applicable | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Provider/platform continuity test | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |

## 13. Maintenance and review

Review this product method when recovery, storage, authentication, audit,
coordination, sink-effect, deployment or reporting interfaces change. Each
deployment reviews its controlled copy after an incident or exercise, after a
material platform/provider change, and at its recorded periodic cadence.

Related operational material:

- [Incident response](../runbooks/incident-response.md) — pipeline operations
  troubleshooting, not the security-incident procedure;
- [Audit Tier-1 violation](../runbooks/audit-tier1-violation.md);
- [Resume failed run](../runbooks/resume-failed-run.md);
- [Scheduler lease recovery](../runbooks/scheduler-lease-recovery.md);
- [Sink-effect recovery](../runbooks/sink-effect-recovery.md);
- [Backup and recovery](../runbooks/backup-and-recovery.md);
- [Database maintenance](../runbooks/database-maintenance.md); and
- [Recovery guarantees](../release/guarantees.md#5-recovery-guarantees).
