# 12 — Privacy impact assessment

**Status:** reusable product assessment complete; deployment record open ·
**Product source reviewed against:** `release/0.8.1` @
`487ac85a377f135e012bb206e3769cd65f6fadb8` (2026-10-01) ·
**Owner:** DTA Cloud Engineering

This document supplies a reusable privacy impact assessment (PIA) method and
records ELSPETH's product-level data handling. It supports a deployment
assessment against the *Privacy Act 1988* and all thirteen Australian Privacy
Principles (APPs). It is a technical mapping, not a conclusion about an
entity's legal obligations or a particular processing activity.

The method follows the [OAIC Guide to undertaking privacy impact assessments](https://www.oaic.gov.au/privacy/privacy-guidance-for-organisations-and-government-agencies/privacy-impact-assessments/guide-to-undertaking-privacy-impact-assessments)
and uses the [OAIC APP Guidelines](https://www.oaic.gov.au/privacy/australian-privacy-principles/australian-privacy-principles-guidelines)
and the [thirteen Australian Privacy Principles](https://www.oaic.gov.au/privacy/australian-privacy-principles/read-the-australian-privacy-principles)
as the reference baseline. Where an agency requires its own form, complete
that form and retain this document as the product evidence attachment.

> **Sensitive when populated:** datasets, affected people, providers,
> countries, contracts, legal conclusions, ratings, recommendations, owners,
> approvals and request records belong in the controlled deployment copy.

## 1. Purpose, scope and method

The deployment assessor must:

1. describe the project, purpose, scope, alternatives and responsible entity;
2. decide the threshold question in § 2 for each execution surface and use
   case;
3. identify affected people and consult the stakeholders in § 3;
4. verify the data/store inventory in § 4 against enabled pipelines and
   integrations;
5. draw the end-to-end flow in § 5, including every disclosure, copy, export,
   backup and deletion path;
6. assess necessity, proportionality and minimisation in § 6;
7. map all thirteen APPs in § 7, recording an applicability rationale even
   where the legal conclusion is “not applicable”;
8. complete the disclosure register, retention plan and rights procedures in
   §§ 8–10;
9. record privacy impacts and recommendations in §§ 11–12; and
10. obtain the required acceptance and re-assess on the triggers in § 13.

Product facts in this document apply to the reviewed source. Deployment facts
must be supported by configuration, provider terms, contracts, data samples,
operating procedures and stakeholder consultation. Evidence about audit and
access controls is cross-linked to [06 — Identity and access](06-identity-and-access.md),
[08 — Logging, audit and monitoring](08-logging-audit-and-monitoring.md) and
[11 — AI / LLM risk assessment](11-ai-llm-risk-assessment.md).

## 2. Threshold assessment

The product-level threshold is conditional by execution surface:

- an authenticated Web deployment necessarily processes personal information
  about users. The Sessions store holds provider and subject, username,
  optional display name and email, organisation identifier where applicable,
  a bounded active-identity claims snapshot, and access/lifecycle timestamps
  and state. The Landscape also records authentication activity with identity
  references, username, request ID, client host and user agent;
- pipeline rows, uploaded files, prompts, model responses, retrieved passages,
  outputs, logs, telemetry, audit payloads, exports and backups can contain
  personal or sensitive information depending on supplied data and enabled
  integrations; and
- a CLI-only deployment may avoid the Web identity data, but must still assess
  its operators, datasets, payload/audit stores, logs, outputs and external
  services.

IdP-provided membership and role attributes are deliberately ignored as authority inputs. They
are not fields in ELSPETH's owned ID-token claims boundary. ELSPETH maintains
its own identity roles and oversight relationships instead. See
[`IdTokenClaims`](../../src/elspeth/web/auth/claims.py) and
[`parse_id_token_claims`](../../src/elspeth/web/auth/id_token.py).

### 2.1 Deployment record — threshold decision

| Question | Required value |
|---|---|
| Project and processing purpose | DEPLOYMENT-TODO: describe the project and each use case |
| Responsible entity and legal context | DEPLOYMENT-TODO: identify the entity and applicable law or policy |
| Execution surfaces in scope | DEPLOYMENT-TODO: record Web, CLI/YAML, scheduled automation and analysis tools |
| Personal information handled | DEPLOYMENT-TODO: identify categories and why they are personal information |
| Sensitive information handled | DEPLOYMENT-TODO: identify categories and approved handling, if any |
| Affected people | DEPLOYMENT-TODO: identify users, data subjects and non-users represented in content |
| PIA threshold conclusion and rationale | DEPLOYMENT-TODO: record the legal/governance conclusion and approver |
| Assessment boundaries and exclusions | DEPLOYMENT-TODO: state inclusions, exclusions, assumptions and dependencies |

## 3. Stakeholders and consultation

At minimum, consider the project owner, privacy officer, security owner, data
custodians, records manager, system operators, identity administrators,
pipeline authors, reviewers, approvers, service desk, incident responders,
provider/contract owners, accessibility specialists, affected program areas
and representatives of affected people. Consultation should test the data map,
necessity, notices, disclosures, rights handling, accessibility, likely harms
and whether the proposed controls are workable.

### 3.1 Deployment record — consultation

| Stakeholder or affected group | Why consulted | Method and date | Findings and response |
|---|---|---|---|
| DEPLOYMENT-TODO: role or group | DEPLOYMENT-TODO: interest or impact | DEPLOYMENT-TODO: method and date | DEPLOYMENT-TODO: finding, decision and evidence |

## 4. Product data and store inventory

“May contain personal information” means the product accepts content whose
privacy character depends on the deployment. It is not a statement that every
record is personal information.

| Data category | Collection or source | Purpose | Product store | Possible recipients / flows | Product retention or deletion mechanics | Personal-information potential |
|---|---|---|---|---|---|---|
| Web identity and profile | Verified local or SSO identity | Authentication, admission, attribution and administration | Sessions `identities` | Authorised administrators; audit projections | Activated, disabled and retired identities remain to anchor history; a stale never-activated pending identity can be explicitly purged under configured retention | Necessarily personal in authenticated Web use [EV-806] [EV-812] |
| Bounded active-identity claims snapshot | Verified IdP claims at activation | Forensics and identity binding | Sessions `identities.raw_claims_json` | Restricted administrative/audit access | Kept with the identity record; IdP-provided membership and role attributes are outside the owned claims boundary | Personal; may include deployment-dependent profile attributes [EV-806] |
| Local credentials | User/admin input and generated reset material | Local authentication | Separate `auth.db` | Authentication and authorised credential administrators | Credential deletion is separate from identity retirement; deployment backups may retain copies | Personal and security-sensitive |
| User-scoped pipeline secrets | Authenticated user input through the Web secret API | Resolve credentials for approved pipeline destinations | Sessions `user_secrets` (name, identity/provider binding, encrypted value, salt, version and timestamps) | Owning user through metadata/create/delete APIs; ELSPETH runtime at resolution; approved external destination at use | Per-secret deletion exists; disabling `user_secrets_enabled` stops use but leaves rows stored; no automatic expiry or identity-retirement cascade; backups may retain copies | Necessarily account-associated and security-sensitive; names can reveal service relationships and values may contain personal information [EV-212] [EV-813] |
| Composer preferences and tutorial progress | Authenticated user's Web interactions | Account-level display choices and resumable tutorial state | Sessions `user_preferences` | Owning user and Web application | PATCH can reset individual fields; no whole-row product deletion or automatic expiry; identity retirement does not cascade | Personal activity data linked to an identity; session/run IDs and source-data hash link to other records [EV-813] |
| Roles and oversight relationships | Administrators and governance workflow | Authorisation, review and accountability | Sessions identity role and relationship tables | Authorised administrators, reviewers and audit | Role grants are retained and revoked rather than deleted so history remains readable | Personal employment/authority information |
| Authentication and administrative events | Web authentication and admin actions | Security monitoring and accountability | Landscape `auth_events` | Authorised audit readers, exports and monitoring | Audit metadata has no product-wide automatic expiry | Personal activity and device/network metadata [EV-807] |
| Audit-grade read and workflow-inspection access evidence | Audit-grade transcript reads and governed workflow inspection | Accountability for access to sensitive Composer evidence | Sessions `audit_access_log` (reader, path, allowlisted query arguments, literal `request.client.host`, timestamp and writer) | Restricted service/audit access and authorised operators | Cascades only with physical session deletion; retained by durable-history soft archive; no standalone product expiry or purge | Necessarily personal: identifies the reader and can contain a literal network address [EV-308] [EV-811] [EV-813] |
| Composer messages and raw/tool audit rows | Authors, providers and tools | Pipeline authoring, audit and recovery | Sessions `chat_messages` and related receipt rows | Authors, authorised reviewers, share-link readers within exposed view, provider during composition | Immutable while the session exists; durable-history session soft-archives, no-durable session can be physically deleted | Frequently personal if users discuss people or upload content [EV-808] |
| Composition states, proposals, reviews and approvals | Authoring and governance actions | Versioning, decision evidence and execution admission | Sessions composition, proposal, interpretation, review, approval and library tables | Authors, reviewers, approvers, share-link readers within exposed view | Durable governance history causes soft archive rather than physical session deletion | Can identify authors/reviewers and contain content-derived information |
| Uploads, managed blobs and session files | User upload or Composer `create_blob` | Authoring inputs and pipeline processing | Managed blob/session directories plus custody metadata | Planner when read, pipeline plugins, reviewers and configured sinks | Physical session deletion purges the blob directory. Durable-history soft archive retains blob metadata and raw files; archived-session blob routes are unavailable, so there is no supported post-archive per-blob purge or automatic expiry | May contain arbitrary personal or sensitive information [EV-808] |
| Source rows, documents and quarantine records | Source systems, files, APIs and users | Pipeline processing and error handling | In-memory processing, payload store, Landscape row/token/error records and configured working storage | Transforms, LLM/search/safety services, sinks, operators and exports | Depends on payload purge and destination; audit metadata can remain after content purge | May contain arbitrary personal or sensitive information |
| Pipeline outputs and sink effects | Transforms, models and deterministic steps | Deliver pipeline results | Landscape evidence, payload store and configured sinks | Sink recipients, reviewers and exports | Sink copies follow destination policy; ELSPETH preserves effect/audit evidence | May contain original, derived or model-generated personal information |
| External-call requests and responses | Pipeline and connected services | LLM, retrieval, web, document analysis and other transforms | Landscape `calls` metadata plus payload store | Configured provider/service; authorised audit readers | Payload blobs default to 90-day purge eligibility and require explicit purge; hashes/metadata remain | Often content-rich and potentially personal [EV-809] [EV-810] |
| Composer/provider call evidence | Planner, advisor and session-title calls | Authoring, advice and usage accounting | Sessions transcript/audit rows and token ledger | Provider and authorised session/audit readers | Follows session archive/delete mechanics; provider copy is separate | User messages and model output may be personal |
| Token and quota usage | Composer and Web-run AI calls | Cost/quota enforcement and accountability | Sessions token ledger and provider-attempt tables | User/admin views and audit | Usage history makes a session durable; schedule is deployment-defined | Links activity and consumption to identities |
| Shareable-review capability and access evidence | Author/reviewer workflow | Read-only external review | Sessions review/share records and operational/audit events | Any active signed-in identity holding a valid capability, within the exposed review view | Expiry, payload removal and global signing-key rotation bound access; there is no recipient binding, one-time use or per-token revocation; retained access evidence follows store policy | Can expose pipeline content and identify participants [EV-811] |
| Application logs, metrics and traces | Runtime components | Operations, security and diagnostics | Deployment log/telemetry back ends | Operators and configured observability providers | Deployment-controlled; full LLM telemetry can contain prompts/responses | Can include identifiers, request metadata and content at higher granularity |
| Exports | Authorised user/operator | Audit analysis, portability and reporting | Chosen export target | Export recipients and later systems | Independent copy; not removed by purging the live payload store | Mirrors selected audit and payload information |
| Backups and replicas | Deployment infrastructure | Recovery and availability | Deployment-managed storage | Infrastructure operators and recovery systems | Independent schedule and deletion lag | Mirrors the stores backed up |
| Provider, search, safety, tracing and sink copies | ELSPETH outbound flows | External processing and delivery | Third-party or agency-managed service | Provider, subprocessors and destination users | Governed by service and contract, not by ELSPETH purge | Same sensitivity as transmitted fields and outputs |

The source-of-truth details for session records are in
[08 § 1.4](08-logging-audit-and-monitoring.md#14-session-composer-and-workflow-audit),
for external calls in [08 § 1.6](08-logging-audit-and-monitoring.md#16-external-calls),
and for AI-specific copies in [11 § 7](11-ai-llm-risk-assessment.md#7-traceability-retention-and-privacy).

### 4.1 Deployment record — actual information handled

Copy the product rows that are in use and add one row per actual dataset or
content class.

| Dataset / information class | People represented | Source and collection method | Purpose and necessity | Classification / sensitivity | Stores | Recipients | Volume and frequency |
|---|---|---|---|---|---|---|---|
| DEPLOYMENT-TODO: dataset or class | DEPLOYMENT-TODO: affected people | DEPLOYMENT-TODO: source and method | DEPLOYMENT-TODO: purpose and necessity | DEPLOYMENT-TODO: approved classification | DEPLOYMENT-TODO: stores and copies | DEPLOYMENT-TODO: recipients | DEPLOYMENT-TODO: scale and cadence |

### 4.2 Deployment record — audit-access network-address policy

The shipped `audit_access_log` writers store the exact string exposed as
`request.client.host`; ELSPETH does not truncate or hash it. The value is
trustworthy as a client address only when the deployment's reverse-proxy and
forwarded-header trust is correct. Physical session deletion cascades the row,
but durable-history soft archive retains it and the product supplies no
independent expiry or purge [EV-813].

This literal-storage choice requires an explicit deployment decision. Record
the necessity and legal basis, authorised readers, proxy-trust configuration,
approved retention, incident use and treatment of backups and exports. A
deployment that cannot approve literal retention requires a product change to
omit, truncate or keyed-hash the value; documentation or configuration cannot
make the current writer do so.

| Decision | Deployment value |
|---|---|
| Literal network-address collection and legal basis | DEPLOYMENT-TODO: |
| Authoritative client-address / trusted-proxy configuration | DEPLOYMENT-TODO: |
| Authorised readers, approved retention and destruction evidence | DEPLOYMENT-TODO: |
| Backups, exports and incident-use treatment | DEPLOYMENT-TODO: |

## 5. Data-flow analysis

Use [02 § 3](02-data-flows-and-trust-boundaries.md#3-product-flows) as the product
flow map and annotate every enabled path with the actual data categories from
§ 4.1. The deployment diagram must show:

1. collection from users, identity providers, files, APIs, source systems and
   retrieval indexes;
2. validation and authoring in the Web Composer or CLI/YAML path;
3. processing through transforms, safety controls and models;
4. persistence in Sessions, Landscape, payload, blob, credential, log and
   telemetry stores;
5. disclosure to providers, managed services, review-link holders and sinks;
6. exports, replicas and backups; and
7. correction, archive, purge, deletion and provider-deletion paths.

For each arrow record purpose, authority, fields, encryption/transport,
country, recipient, persistence and whether a person can prevent or contest
the flow.

### 5.1 Deployment record — flow verification

| Flow / diagram reference | Data categories | Authority and purpose | Destination and country | Persistence / onward flow | Verified by, date |
|---|---|---|---|---|---|
| DEPLOYMENT-TODO: flow ID | DEPLOYMENT-TODO: fields or categories | DEPLOYMENT-TODO: authority and necessity | DEPLOYMENT-TODO: service, endpoint and country | DEPLOYMENT-TODO: copies and onward disclosure | DEPLOYMENT-TODO: verifier and date |

## 6. Necessity, proportionality and minimisation

ELSPETH offers product controls that can support minimisation: authors declare
the fields visible to prompt and retrieval templates; Web LLM destinations are
operator-profiled; Composer redaction removes internal paths and sensitive
arguments; a bounded detector refuses recognised credential material in the
Web and Composer control plane before provider or persistence effects;
author-set tracing is refused for Web pipelines; and payload content can be
purged while hashes and metadata remain. The detector is not universal DLP and
does not scan pipeline rows, inline blob bodies, runtime model data, telemetry
content or sink output [EV-227]. These controls do not choose the minimum
necessary fields or prove that AI processing is necessary.

The deployment assessment must challenge:

- whether the purpose can be achieved without personal information, without
  sensitive information, or without AI;
- whether identifiers can be removed, tokenised, generalised or processed
  locally before external disclosure;
- whether prompts, uploads, retrieval results, logs and exports carry fields
  unrelated to the purpose;
- whether output needs to be stored, shared or linked back to an identity;
- whether lower telemetry granularity and shorter retention meet the need;
- whether test data can be synthetic or de-identified; and
- whether the benefit is proportionate to impacts on users and non-users.

### 6.1 Deployment record — minimisation decisions

| Processing element | Necessity and alternative considered | Minimum fields / precision / volume | Minimisation or redaction control | Decision evidence |
|---|---|---|---|---|
| DEPLOYMENT-TODO: element | DEPLOYMENT-TODO: necessity and alternatives | DEPLOYMENT-TODO: minimum data | DEPLOYMENT-TODO: control | DEPLOYMENT-TODO: evidence and decision maker |

## 7. Deployment record — Australian Privacy Principles mapping

Every deployment completes the “Applicability and outcome” column. The product
observations identify questions and mechanics; they are not legal findings.

| APP | Product observation | Deployment assessment required | Applicability and outcome |
|---|---|---|---|
| APP 1 — open and transparent management | Product docs expose data flows and controls but do not supply an organisation's privacy policy, contact point or practices | Identify responsible entity, notices, policy, complaint path, PIA governance and change control | DEPLOYMENT-TODO: rationale, evidence, gap and treatment |
| APP 2 — anonymity and pseudonymity | Authenticated Web use requires an identity and attribution. CLI-only processing may use different operator controls | Decide where anonymous/pseudonymous interaction is practicable and document why identity is necessary elsewhere | DEPLOYMENT-TODO: rationale, evidence, gap and treatment |
| APP 3 — collection of solicited personal information | Web identity fields are collected for authentication/admission; arbitrary pipeline content is deployment-defined | Establish lawful necessity, collection authority, minimum fields and any sensitive-information condition | DEPLOYMENT-TODO: rationale, evidence, gap and treatment |
| APP 4 — unsolicited personal information | Free text, uploads, source rows, retrieval results and model output can introduce unexpected personal information | Define detection, triage, quarantine, deletion and records handling for unsolicited content | DEPLOYMENT-TODO: rationale, evidence, gap and treatment |
| APP 5 — notification of collection | No deployment privacy/collection notice is supplied by the product | Place suitable notice in the IdP/application and any indirect-collection workflow; cover purposes, disclosures, access/correction and consequences | DEPLOYMENT-TODO: rationale, notice and treatment |
| APP 6 — use or disclosure | Product supports providers, managed services, review links, sinks, telemetry, exports and MCP/audit access | Match every secondary use and disclosure to purpose/authority; complete § 8 | DEPLOYMENT-TODO: rationale, evidence, gap and treatment |
| APP 7 — direct marketing | ELSPETH has no product direct-marketing purpose | Confirm no pipeline, export or integrated destination introduces direct marketing | DEPLOYMENT-TODO: rationale, evidence, gap and treatment |
| APP 8 — cross-border disclosure | Provider, tracing, search, safety, telemetry and sink locations are operator/deployment choices | Determine countries, recipients, onward transfers, contractual controls and applicable APP 8 mechanism | DEPLOYMENT-TODO: rationale, evidence, gap and treatment |
| APP 9 — government-related identifiers | The product identity model does not adopt IdP-provided membership or role attributes as authority, but arbitrary content can contain government-related identifiers | Identify such identifiers and restrict adoption, use and disclosure to the approved purpose | DEPLOYMENT-TODO: rationale, evidence, gap and treatment |
| APP 10 — quality of personal information | IdP refresh/rebinding and model-generated or transformed personal information present different accuracy risks | Define source-of-truth, validation, review, currency, correction propagation and controls against treating generation as fact | DEPLOYMENT-TODO: rationale, evidence, gap and treatment |
| APP 11 — security of personal information | Access control, audit, redaction, payload hashing/purge and encrypted secret storage support protection; external copies, backups and retention remain deployment-controlled | Assess threats, least privilege, encryption, monitoring, destruction/de-identification and incident handling | DEPLOYMENT-TODO: rationale, evidence, gap and treatment |
| APP 12 — access | Product access paths differ by store: user/admin Web views, authorised audit views, `elspeth explain`, MCP analysis, exports and provider portals | Define identity verification, search, collation, redaction, exceptions, response and secure delivery | DEPLOYMENT-TODO: rationale, evidence, gap and treatment |
| APP 13 — correction | Identity source data, immutable transcript/audit rows, mutable content stores and external copies have different correction mechanics | Define authoritative correction, annotations, downstream/provider propagation and notice of refusal | DEPLOYMENT-TODO: rationale, evidence, gap and treatment |

## 8. Disclosure and cross-border record

The product flow classes include identity providers; Composer planner,
advisor and session-title providers; pipeline LLMs; document-analysis,
search, prompt-shield and content-safety services; web fetch targets; sinks;
tracing and telemetry; shareable-review holders; exports; and local MCP audit
analysis. Cross-link model/provider suitability to
[11 § 3](11-ai-llm-risk-assessment.md#3-provider-and-data-suitability) rather
than duplicating approval evidence.

### 8.1 Deployment record — disclosure register

Create one row per recipient/service and per materially different purpose.

| Recipient / service / provider | Purpose and flow ID | Data categories | Endpoint, region and countries | Contract and subprocessors | Training/reuse and provider retention | Onward disclosure / deletion | APP 6 and APP 8 decision |
|---|---|---|---|---|---|---|---|
| DEPLOYMENT-TODO: recipient | DEPLOYMENT-TODO: purpose and flow | DEPLOYMENT-TODO: transmitted fields | DEPLOYMENT-TODO: endpoint and locations | DEPLOYMENT-TODO: terms and subprocessors | DEPLOYMENT-TODO: terms | DEPLOYMENT-TODO: controls and request path | DEPLOYMENT-TODO: mechanism, rationale and approval |

## 9. Retention, archive and deletion

Product retention mechanisms differ by store and must not be collapsed into a
single period.

| Store or copy | Retention trigger / product default | Enforcement | Deletion or archive behaviour | What remains | Technical limitation |
|---|---|---|---|---|---|
| Payload-store bodies | Eligible after `payload_store_retention_days`, default 90 days | Explicit `elspeth purge`; not an automatic timer | Expired payload blobs are removed | Landscape metadata and hashes remain; explain reports content unavailable | Purge must be operated and does not remove external, export or backup copies [EV-809] |
| Landscape audit records | Run, call, row, token, auth and effect events | No product-wide automatic expiry | Payload references can become unavailable after purge; audit metadata remains | Identity, timing, hashes, status and other audit fields | Correction/deletion can conflict with audit integrity and records obligations; deployment decision required |
| Composer sessions and transcripts | Session archive request | Database decision under session fence | Durable-history session is soft-archived and retains transcript; no-durable session can be physically deleted | Durable authoring, usage, run, review and approval evidence remains after soft archive | Transcript content is immutable while the session exists [EV-519] [EV-808] |
| Activated/retired identity, role and relationship records | Administrative lifecycle actions | Disable/retire/revoke operations | Activated, disabled and retired identities are retained to anchor audit; role history is revoked rather than deleted | Identity and authority history | Audit and authority history remains subject to the deployment's records and privacy decision [EV-113] |
| Never-activated pending identities | `identity_pending_retention_days`, default 90 days | Explicit admin-only `POST /api/auth/admin/identities/purge-pending`; not an automatic timer | Deletes rows strictly older than the database-clock cutoff, at most 200 per call | Bounded summary and one attributable Landscape event per deleted identity; response returns exact IDs and `has_more` | Does not remove provider, export, log or backup copies; activated, disabled and dormancy-re-pended identities are ineligible [EV-812] |
| Local credentials | Administrative credential lifecycle | Authorised credential administration | Credential can be deleted separately; a bound identity is retired and its audit history remains | Identity/audit history and deployment backups | Removing a credential does not erase other stores |
| User-scoped secrets | Explicit user create/update; no age trigger | Authenticated per-secret DELETE | Deletes the live Sessions row; disabling user secrets does not delete stored rows | Audit fingerprints/history and backups may remain | No automatic expiry or identity-retirement cascade; address dormant/retired accounts and backups [EV-813] |
| Composer preferences and tutorial progress | First preference/tutorial write; no age trigger | GET/PATCH only | Individual nullable fields can be cleared, but no whole-row deletion exists | Identity-linked row, update time and uncleared tutorial linkage | No automatic expiry; identity retirement is not a purge [EV-813] |
| Audit-access log, including literal network address | Audit-grade or governed inspection read; no age trigger | Session FK cascade only | Removed with physical session deletion; retained on durable-history soft archive | Reader identity, path, allowlisted query arguments, literal client-host string, time and writer | No standalone expiry/purge; deployment retention cannot presently be enforced independently [EV-813] |
| Managed blobs and session files | User/author actions and session lifecycle; no age trigger | Per-blob operations while live; fenced session archive | Physical deletion purges the raw session blob directory; soft archive retains blob rows and raw files | On soft archive, filename, provenance, hashes and complete raw content remain | Archived sessions cannot use normal blob routes; no product TTL or post-soft-archive purge, so retention may be indefinite [EV-808] |
| Logs and telemetry | Deployment logging configuration | Deployment/logging platform | Platform-specific expiry or deletion | Aggregates or archives may remain | Higher granularity can create content copies outside product purge |
| Provider/search/safety/tracing/sink copies | External service receipt | Provider contract and portal/API | Provider-specific | Provider logs, abuse-monitoring or backups may remain | ELSPETH cannot enforce provider deletion |
| Exports and backups | Export/backup operation | Deployment process | Independent deletion and media lifecycle | Copies may remain after source deletion | Restore can reintroduce deleted information unless procedures prevent it |

See [guarantees § 1.4](../release/guarantees.md#14-payload-retention) for the
payload integrity promise and [08 § 1.4](08-logging-audit-and-monitoring.md#14-session-composer-and-workflow-audit)
for session archive mechanics.

### 9.1 Deployment record — retention schedule

| Store / copy | Approved period and trigger | Records authority / legal basis | Enforcement owner and job | Exceptions / holds | Destruction evidence and backup propagation |
|---|---|---|---|---|---|
| DEPLOYMENT-TODO: store | DEPLOYMENT-TODO: period and trigger | DEPLOYMENT-TODO: authority and basis | DEPLOYMENT-TODO: owner, command/job and cadence | DEPLOYMENT-TODO: hold or exception | DEPLOYMENT-TODO: evidence and propagation |

## 10. Access and correction procedures

### 10.1 Product mechanics

| Information | Product access/export path | Product correction/deletion mechanic | Constraint to address in procedure |
|---|---|---|---|
| Identity/profile | Authorised identity/admin views and audit | Update from the authoritative identity process; disable/retire identity | IdP source-of-truth and retained identity/audit history must be reconciled |
| Roles and relationships | Authorised administration and audit events | Revoke or supersede; history remains | Historical authority records should not be rewritten as though they never occurred |
| User-scoped secrets | Owning user can list names/availability; plaintext is never returned by inventory APIs | Authenticated per-secret replacement or deletion | Identity retirement and feature disablement do not erase rows; audit fingerprints and backups are separate copies |
| Composer preferences/tutorial progress | Authenticated GET | PATCH can clear supported fields; no whole-row delete | Account retirement preserves the row; related session/run records may remain |
| Audit-access evidence | No general end-user export/correction surface; authorised database/audit procedure required | No standalone correction/deletion; physical parent-session deletion cascades | Do not rewrite evidence as if the read did not occur; address literal-IP necessity, accuracy, retention and annotation explicitly |
| Managed blobs in a soft-archived session | No normal blob API access after archive | No supported post-soft-archive per-blob delete | Raw content remains for session/audit consistency; privacy deletion requires a future evidence-preserving mechanism, not direct filesystem/SQL edits |
| Composer transcript and decisions | Authorised transcript/review views; controlled share links | Message bodies cannot be edited while the session exists; append an explanation/correction or physically delete only when no durable history permits | A correction must remain distinguishable from the original evidence |
| Pipeline source/output content | `elspeth explain`, Web inspection, MCP analysis and exports according to authority | Correct authoritative source and rerun; purge eligible payload body when allowed | Audit hashes/metadata and downstream sinks may retain the original event |
| Model-generated personal information | As pipeline output, call payload or downstream sink record | Human verification, correction at the destination, annotation and controlled rerun | Model output is not an authoritative source merely because it is recorded |
| Logs/telemetry | Observability platform search/export | Platform-specific redaction/deletion | Correlation data, replicas and provider copies may persist |
| External provider/sink copy | Provider portal/API or recipient process | Contractual/provider request path | ELSPETH cannot prove or enforce external correction/deletion by itself |

### 10.2 Deployment record — access request procedure

| Step | Required value |
|---|---|
| Intake, identity verification and authorised decision maker | DEPLOYMENT-TODO: record channel, verification and authority |
| Stores and external services searched | DEPLOYMENT-TODO: map request types to every store in §§ 4 and 8 |
| Search, export, review and redaction method | DEPLOYMENT-TODO: record tools, secure workspace and review controls |
| Exceptions, third-party information and refusal handling | DEPLOYMENT-TODO: record legal decision and notice process |
| Secure response and completion evidence | DEPLOYMENT-TODO: record channel, time target and evidence retained |

### 10.3 Deployment record — correction request procedure

| Step | Required value |
|---|---|
| Authoritative source and decision maker | DEPLOYMENT-TODO: identify who determines accuracy |
| Live-store correction or appended annotation | DEPLOYMENT-TODO: identify mechanic for each store |
| Immutable audit/transcript treatment | DEPLOYMENT-TODO: define annotation and linkage without rewriting history |
| Downstream, provider, export and backup propagation | DEPLOYMENT-TODO: define notification and correction/deletion path |
| Refusal, review and contestability | DEPLOYMENT-TODO: record reasons, notice, escalation and service target |

## 11. Privacy impacts and risk assessment

Assess impacts on people, not only compliance gaps. Consider loss of control,
unexpected secondary use, overseas exposure, sensitive inference, identity or
relationship disclosure, inaccurate model-generated information, exclusion,
surveillance, re-identification, inaccessible notices, inability to contest,
retention beyond expectation, breach and chilling effects.

Do not assign product-wide ratings. Likelihood, consequence, existing-control
effectiveness and residual acceptance depend on actual data and use.

### 11.1 Deployment record — privacy impact register

| ID | Affected people and impact | Cause / flow | Existing controls and evidence | Likelihood | Consequence | Treatment | Owner / due date | Residual decision and approval |
|---|---|---|---|---|---|---|---|---|
| DEPLOYMENT-TODO: ID | DEPLOYMENT-TODO: people and impact | DEPLOYMENT-TODO: cause and flow | DEPLOYMENT-TODO: control and evidence | DEPLOYMENT-TODO: rating and rationale | DEPLOYMENT-TODO: rating and rationale | DEPLOYMENT-TODO: action | DEPLOYMENT-TODO: owner and date | DEPLOYMENT-TODO: decision and approval |

## 12. Reusable recommendations and response

The following are baseline deployment requirements unless the controlled PIA
documents a reasoned alternative:

1. minimise or redact content before external AI, retrieval, safety, tracing,
   telemetry and sink calls;
2. approve each provider's region, subprocessors, contract, training/reuse,
   abuse-monitoring, retention and deletion terms;
3. keep content-rich tracing and telemetry disabled until its purpose,
   recipients, access and retention have been assessed;
4. configure and operate payload purge, pending-identity purge, per-user secret
   retirement, session archive, audit-access/IP retention, managed-blob
   retention, log expiry, provider deletion and backup disposal as separate
   controls; record where no automatic or selective purge exists and do not
   claim archive as deletion;
5. restrict administrative, audit, MCP, export and share-link access and review
   it periodically;
6. provide collection/privacy notices in the Web/IdP workflow and at indirect
   collection points;
7. establish the access, correction, annotation, archive, deletion and refusal
   procedures in § 10;
8. prohibit unapproved sensitive-information use and review unexpected
   personal information under an unsolicited-information procedure;
9. treat model-generated personal information as unverified until an
   authoritative source or trained reviewer confirms it; and
10. connect privacy complaints and breaches to the incident-response plan,
    provider notification and affected-person communication; and
11. before enabling authenticated Web use, approve or remediate literal
    `audit_access_log` network-address storage and durable-history retention of
    raw managed blobs; record inability to meet the destruction schedule as a
    privacy risk rather than an implemented control.

### 12.1 Deployment record — recommendation response

| Recommendation | Response / selected control | Owner | Due date | Status and evidence | Accepted residual issue / approver |
|---|---|---|---|---|---|
| DEPLOYMENT-TODO: recommendation or baseline number | DEPLOYMENT-TODO: response | DEPLOYMENT-TODO: owner | DEPLOYMENT-TODO: date | DEPLOYMENT-TODO: status and evidence | DEPLOYMENT-TODO: controlled risk reference and approval |

## 13. Governance, review and change triggers

The controlled PIA should record version, assessor, consultations, privacy and
security review, accountable approval, recommendation status and links to the
system boundary, threat model, AI assessment, records authority and incident
plan. Keep superseded PIAs according to the approved records schedule.

Review the PIA before or promptly after:

- adding a dataset, category of personal/sensitive information, affected group
  or materially different purpose;
- enabling a new provider, country, subprocessor, model, retrieval source,
  safety service, sink, tracing/telemetry exporter, share mechanism or export;
- changing identity fields, access roles, disclosure, retention, archive,
  deletion, backup, access or correction mechanics;
- increasing automation or consequence, removing human review, or changing the
  explanation/contest path;
- a privacy incident, significant complaint, inaccurate or discriminatory
  outcome, repeated rights-request failure, or control failure; or
- a change in law, OAIC guidance, agency policy, contract or records authority.

### 13.1 Deployment record — completion and approval

| Record | Required value |
|---|---|
| PIA version, date and assessed release/image | DEPLOYMENT-TODO: record identifiers and immutable release evidence |
| Assessor and consulted stakeholders | DEPLOYMENT-TODO: record people/roles and consultation evidence |
| Privacy, security, records, accessibility and program reviews | DEPLOYMENT-TODO: record reviewers and outcomes |
| Recommendation disposition | DEPLOYMENT-TODO: link § 12.1 and controlled risks |
| Approval and conditions | DEPLOYMENT-TODO: record accountable approver, date and conditions |
| Next scheduled review | DEPLOYMENT-TODO: record date or event |
