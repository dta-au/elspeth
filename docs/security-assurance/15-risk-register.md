# 15 — Risk register

**Status:** public schema complete — populated register held in the controlled
deployment copy · **Reviewed against:** `release/0.8.1` candidate @
`eee4bb941` (2026-10-01) · **Owner:**
ELSPETH maintainer

> **Do not populate this file in the public repository.** Open risks,
> exposure conditions, likelihood, treatment detail and acceptance decisions
> may help an attacker. Keep the populated register and its evidence in the
> controlled deployment copy. Public product documents refer to open risks
> only by stable opaque ID.

This file defines the minimum record, import procedure, reconciliation rules,
lifecycle and deployment decisions for the controlled register. It does not
record a risk decision for this device or any other installation.

## 1. Stable identifiers and record rules

- Use `R-nnn` identifiers. Preserve every risk ID assigned by a source
  document; never renumber or reuse an ID, including after closure.
- `R-001` through `R-024` are reserved by
  [04](04-threat-model.md). Create controlled records for every `Residual`
  reference. Record an applicability disposition for every `Deployment`
  reference so each public pointer remains resolvable.
- More than one threat may map to one risk only when it has one causal risk
  statement, treatment and acceptance decision. Preserve every source threat
  ID in that record.
- When risks are split, mark the original `Superseded` and record every
  successor ID. When risks are merged, keep each original ID as a resolvable
  alias with the canonical ID. Do not delete history.
- Allocate new IDs from the next unused number in the controlled register.
  Record the allocator, date and source. Do not publish the populated title or
  statement merely because the ID is public.
- Write the risk statement in cause–event–consequence form: *because of
  [cause], [event] may occur, resulting in [consequence]*. Name affected assets
  and confidentiality, integrity or availability objectives separately.
- Treat `Accepted` as an active decision with scope, authority and expiry;
  it is not closure. A risk becomes `Closed` only when evidence shows its
  closure criteria are met and an independent reviewer verifies the result.

Final **Status** values are `Open`, `Treatment in progress`,
`Pending acceptance`, `Accepted`, `Closed`, `Not applicable`, and
`Superseded`. Final **Treatment option** values are `Mitigate`, `Avoid`,
`Transfer`, and `Accept`.

## 2. Deployment record — register custody and rating method

Every `DEPLOYMENT-TODO:` in this document is intentionally unpopulated in the
public repository.

| Field | Deployment value |
|---|---|
| Controlled register location and immutable register identifier | DEPLOYMENT-TODO: |
| Register security classification and access-control group | DEPLOYMENT-TODO: |
| Register owner and deputy | DEPLOYMENT-TODO: |
| Assessed release, deployment and assessment-boundary reference | DEPLOYMENT-TODO: |
| Risk matrix name, owner, version and approval date | DEPLOYMENT-TODO: |
| Likelihood scale and assessment horizon | DEPLOYMENT-TODO: |
| Consequence scale and security objectives | DEPLOYMENT-TODO: |
| Matrix combination rule and any override rule | DEPLOYMENT-TODO: |
| Risk appetite, tolerance and escalation thresholds | DEPLOYMENT-TODO: |
| Authorised accepting-authority role and named holder | DEPLOYMENT-TODO: |
| Governing acceptance policy and delegation instrument | DEPLOYMENT-TODO: |
| Delegated accepting authorities and maximum rating / scope | DEPLOYMENT-TODO: |
| Maximum acceptance period and default review cadence | DEPLOYMENT-TODO: |
| Independent closure-verification role | DEPLOYMENT-TODO: |
| Controlled evidence-store location | DEPLOYMENT-TODO: |

The risk matrix must define how to rate safety, financial, privacy,
reputational or regulatory consequences if those dimensions are relevant.
Record any expert override with its rationale and approving authority; do not
silently adjust a calculated rating.

## 3. Deployment record — risk analysis

Keep one row per stable risk ID. The source references column may contain
multiple threat, control, finding or issue IDs, but the structured statement
must still describe one coherent risk. Qualify every reference by source, for
example `04:T-030`, `05:ism-2026-06::1234` or `14:PEN-2026-07`; an
unqualified control or finding ID is not a stable cross-source identity.

| ID | Controlled title and cause–event–consequence statement | Affected assets / security objectives | Scope and assumptions | Source references | Existing controls and evidence | Control owner | Control effectiveness | Inherent likelihood | Inherent consequence | Inherent rating |
|---|---|---|---|---|---|---|---|---|---|---|
| DEPLOYMENT-TODO: stable `R-nnn` | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |

Assess inherent risk before crediting existing controls. Then assess control
design and operating evidence before deriving the current residual rating in
§ 4. A design description without deployment evidence cannot by itself prove
that an inherited or operator control is effective.

## 4. Deployment record — treatment, residual risk and disposition

| ID | Treatment option and actions / milestones | Treatment owner | Target date | Status | Current residual likelihood | Current residual consequence | Current residual rating | Target residual rating | Accepting authority / decision date | Acceptance scope / rationale / expiry | Last review | Next review / trigger | Closure criteria and verification evidence | Closure or successor reference |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DEPLOYMENT-TODO: stable `R-nnn` | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |

Apply these validity rules:

- `Treatment in progress` requires an action, owner, target date and target
  residual rating.
- `Pending acceptance` and `Accepted` require a current residual assessment.
- `Accepted` requires the authorised person or role, decision date, scope,
  rationale, expiry and review trigger. Acceptance outside delegated scope is
  invalid.
- An expired acceptance returns to `Pending acceptance` until it is renewed,
  treated or closed.
- `Not applicable` is permitted for a deployment-conditional source only when
  the deployment decision and evidence are recorded. It does not free the ID
  for reuse.
- `Closed` requires closure criteria, dated verification evidence and the
  independent verifier. Keep residual and treatment history intact.
- `Superseded` requires successor or canonical IDs; every source reference
  must remain traceable.

## 5. Sources to import

Reconcile each source population, including a zero population, rather than
assuming that silence means there were no findings:

1. Every `Residual` threat and opaque risk reference in
   [04](04-threat-model.md).
2. Every `Deployment` threat in document 04 after recording whether the
   installation makes it applicable. Retain a `Not applicable` disposition
   where it does not.
3. Every residual AI / LLM risk in
   [11](11-ai-llm-risk-assessment.md).
4. Every `Partially implemented` and `Not implemented` control in
   [05](05-statement-of-applicability.md), every deficient control-effectiveness
   or assessment result, and any inherited/shared control whose evidence or
   responsibility is incomplete.
5. Identity, access and authorisation gaps or deficient deployment controls
   from [06](06-identity-and-access.md).
6. Secret, key-custody, rotation and compromise gaps from
   [07](07-secrets-and-key-management.md).
7. Logging, audit, monitoring and evidence-integrity gaps from
   [08](08-logging-audit-and-monitoring.md).
8. Privacy threshold, impact and treatment findings from
   [12](12-privacy-impact-assessment.md).
9. Internal security reviews, independent assessments, penetration tests and
   verification failures recorded by
   [14](14-security-testing.md).
10. Vulnerability and supply-chain exceptions, accepted scanner suppressions
   and overdue remediation tracked under
   [10](10-vulnerability-and-supply-chain.md).
11. Open security-labelled issues and private vulnerability reports. Use a
   controlled source reference; do not copy exploit-enabling detail into a
   public issue.
12. Incidents, near misses, provider advisories, architecture reviews,
   readiness failures and deployment drift that change likelihood,
   consequence or control effectiveness.

### 5.1 Public submission-item dispositions at the reviewed baseline

These are assurance-scope dispositions, not populated deployment risks or
risk acceptances. A controlled deployment copy reopens an item if its actual
scope or evidence differs.

| Assessment item | Disposition | Evidence and scope condition |
|---|---|---|
| Configured pending-identity retention had no production invocation path | Closed at the reviewed product baseline | Admin-only explicit purge applies configured retention, returns and audits the exact bounded deletion result, and has unit and PostgreSQL contention evidence [EV-812] |
| Active identities could author or run without the intended `user` role | Closed at the reviewed product baseline | Live same-provider human unscoped `user` authority is required at owner workload routes and re-proved at chargeable and ticket boundaries; reserved roles remain non-authorising [EV-104] |
| Unattended machine-to-Web-API access | Resolved by explicit 0.8.1 assessed-use-case exclusion | `service` remains reserved and unimplemented. No service credential is claimed, and a human bearer token is not a service credential. A future in-scope machine integration requires a new control and risk assessment [EV-104] |
| Optional reference gateway dependency and release boundary | Source remediation closed; operating evidence conditional on scope | The gateway now has a frozen lock/update path, required dependency, licence, image and conformance checks, and an exact-digest publication contract. An in-scope deployment still needs the successful GitHub/publication and registry evidence named in documents 10 and 14 [EV-722] |
| Unmatched PyJWT audit exception | Closed at the reviewed product baseline | The stale exception was removed; the current frozen audit has no PyJWT finding and the remaining dated ChromaDB exceptions stay under the documented review policy [EV-708] |
| Credential material at Web and Composer control boundaries | Product remediation closed for the defined control plane; `R-008` remains applicable to excluded data-plane content | A versioned bounded detector and atomic guards cover the documented Web request, Composer state/tool/provider/publication/approval/execution surfaces. It is not universal DLP and excludes pipeline rows, inline blob bodies, runtime model data, telemetry content and sink output [EV-227] |
| Additional judge lint and quality workflow absent from required checks | Not a defect; advisory by design | The workflow remains outside the standard enforcement and Git branch-protection package [EV-611] |

De-duplicate only after import. A duplicate disposition must name the
canonical risk ID and preserve every source reference. A finding assigned to
engineering work still remains in the register until its risk disposition and
verification are complete.

## 6. Deployment record — source reconciliation

Obtain counts from the authoritative source register, assessment output, API
or database. Record the instrument and use a known-positive and known-negative
control before relying on a zero count.

| Source population | Source version / query | Candidate count | Imported risk IDs | Duplicate-to-canonical mappings | Not-applicable dispositions | Unresolved omissions | Checked by / date |
|---|---|---|---|---|---|---|---|
| Document 04 `Residual` references | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Document 04 `Deployment` references | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Document 11 residual AI / LLM risks | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Document 05 partial / not-implemented / deficient effectiveness or assessment results / incomplete inherited/shared controls | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Document 06 identity and access gaps | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Document 07 secret and key-management gaps | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Document 08 logging, audit and monitoring gaps | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Document 12 privacy findings | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Document 14 internal and independent test findings | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Document 10 vulnerability and supply-chain exceptions | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Security issues and private reports | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Incidents, provider advisories, reviews and deployment drift | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |

The import gate passes only when every source candidate resolves to a risk ID,
a duplicate mapping, or an evidenced not-applicable disposition, and
`Unresolved omissions` is zero for every row. The set of `R-nnn` identifiers
in §§ 3 and 4 must match, excluding only documented aliases whose canonical
record is present.

## 7. Review and change control

Review the affected risk records when any of these events occurs:

- the assessed commit, image digest or deployment architecture changes;
- tenancy, classification, data type, user population or exposure changes;
- an identity, provider, model, connector or inherited platform control
  changes;
- a control's implementation or operating evidence changes;
- a security test, vulnerability report, incident or audit produces new
  evidence;
- a treatment target, acceptance expiry or scheduled review becomes due.

Record each review date, reviewer, evidence considered, rating change and
decision. Keep an append-only decision history or an equivalent versioned
audit trail in the controlled repository.

## 8. Deployment record — current review and authorisation

| Field | Deployment value |
|---|---|
| Register review cut-off and version | DEPLOYMENT-TODO: |
| Open / treating / pending-acceptance / accepted / closed counts | DEPLOYMENT-TODO: |
| Overdue treatment and review counts | DEPLOYMENT-TODO: |
| Highest current residual rating and escalation outcome | DEPLOYMENT-TODO: |
| Source-reconciliation gate result | DEPLOYMENT-TODO: |
| Acceptance-authority review result | DEPLOYMENT-TODO: |
| Authorisation decision and conditions | DEPLOYMENT-TODO: |
| Next scheduled review | DEPLOYMENT-TODO: |
