# 14 — Security testing

**Status:** product testing baseline and findings follow-up method complete;
independent penetration testing record open · **Reviewed against:** `release/0.8.1`
@ `004c0eee0` (2026-09-30) · **Owner:** ELSPETH maintainer

Records the security testing carried out, who did it, what was in scope and
what was found. Separate internal review from independent testing — an
assessor weighs them differently.

> **Sensitive when populated:** detailed findings and reproduction steps for
> unfixed issues must not be committed to this public repository. Record the
> test, scope and summary outcome here; hold the report in the controlled
> copy.

## 1. Testing record

| Date | Type | Performed by | Independent? | Scope | Release tested | Outcome summary | Report location |
|---|---|---|---|---|---|---|---|
| 2026-09-23 | Scoped internal plan and implementation security review | ELSPETH maintainer with four independent AI review lanes | No — separate agent contexts, not organisational independence | Judge-metadata secret custody and signature verification; sentinel identity binding; affected export, persistence and historical projections; related regression and policy gates | `release/0.8.1`; baseline `ee04378f8`, with repaired line reviewed through `a6803f2e4` | Required changes were implemented and independently re-read. Focused and PostgreSQL checks passed and the controlled lint comparison added no findings. The frozen broad gate remained a failed run reconciled by bounded repairs, and authoritative operator signature verification was not performed; the record makes both limits explicit | [Pre-publication security plan review](../reviews/2026-09-23-pre-publication-security-review.md) [EV-623] |

## 2. Continuous testing

The complete gate definitions, trigger conditions and measured inventories
are in [09 — Secure development lifecycle](09-secure-development-lifecycle.md).
The security-relevant layers are:

| Layer | Product signal | Enforcement |
|---|---|---|
| Static source analysis | CodeQL scans Python with the `security-extended` query suite on pushes, pull requests and a weekly schedule. Ruff, strict mypy, contract checks and the 24-rule `elspeth-lints` registry add project-specific integrity checks [EV-608] [EV-612] | CodeQL and the `CI Success` aggregate block merge to `main` under the measured ruleset |
| Dependency and licence analysis | `pip-audit --strict` separately checks the frozen root and gateway Python graphs; `npm audit` checks both npm lockfiles at every severity; the licence job rejects GPL and AGPL dependencies in both Python graphs [EV-707] [EV-720] [EV-722] | The dependency-and-licence job feeds required `CI Success`; documented exceptions follow [10 § 2](10-vulnerability-and-supply-chain.md#2-triage-and-remediation-targets). A separate required gateway lane scans the assembled image and runs it read-only through external conformance |
| Python behaviour | The default selection exercises unit, integration, property, invariant and end-to-end behaviour on Python 3.12 and 3.13. A separate required serial suite tests schema, SQL and contention against PostgreSQL [EV-618] [EV-619] | 59,486 default items and 652 PostgreSQL-marked items were collected at the reviewed commit; both lanes feed `CI Success` |
| Browser and frontend behaviour | Vitest plus TypeScript type checking cover frontend components; Playwright runs browser journeys against a real backend and Chromium [EV-620] | Both frontend jobs feed `CI Success` |
| Security policy and release integrity | Redaction-direction governance, telemetry attribution, trust-tier analysis, the local secret scanner, additional advisory judge lint and quality signals, required-check verification, image smoke tests, keyless image signing, provenance and SBOM generation cover policy and release seams. The gateway workflow-source contract adds exact-digest amd64/arm64 scans, attestation retrieval, signature verification and read-only smoke testing | [09 § 2.2](09-secure-development-lifecycle.md#22-gate-table) states which signals block a local commit, merge or publication and which remain advisory |

Continuous testing has defined limits. CodeQL currently scans Python rather
than every language and artifact. Live-provider tests run only on trusted
pushes or manual workflows, and mutation testing is advisory. The gateway
dependency and built-image controls are present in source and passed their
reviewed local qualification. No GitHub CI run at this local commit has yet
confirmed the required gateway job, and no publication run has supplied
registry, multi-platform scan, attestation, signature and smoke evidence for a
published gateway digest. Automated and AI-assisted review are not substitutes
for an independent penetration test.

## 3. Deployment record — independent penetration testing

No completed independent penetration test is evidenced in the product
repository. Complete this record for the assessed installation and keep the
detailed report in the controlled copy.

| Item | Value |
|---|---|
| Provider and assessor; relationship to the maintainer and deploying organisation | DEPLOYMENT-TODO: |
| Scope and rules of engagement, including included interfaces, excluded providers and authorised denial-of-service limits | DEPLOYMENT-TODO: |
| Test date or window | DEPLOYMENT-TODO: |
| Release, commit and container digest tested | DEPLOYMENT-TODO: |
| Target environment and written test authorisation | DEPLOYMENT-TODO: controlled copy only |
| Outcome or publishable executive summary, including finding counts by severity | DEPLOYMENT-TODO: |
| Full report location and access control | DEPLOYMENT-TODO: controlled copy only |
| Remediation retest date, scope and outcome | DEPLOYMENT-TODO: |

## 4. Findings follow-up

1. **Receive sensitive findings privately.** Use the channels and handling
   rules in [SECURITY.md](../../SECURITY.md); do not publish exploit details,
   secrets, personal data or proof-of-compromise material in an issue.
2. **Triage and contain.** The ELSPETH maintainer assigns the highest credible
   impact and applies the release-blocking and remediation policy in
   [10 § 2](10-vulnerability-and-supply-chain.md#2-triage-and-remediation-targets).
   Preserve affected evidence and withdraw or disable an unsafe artifact when
   containment requires it.
3. **Choose the tracking record.** Track publishable product work in the
   repository issue tracker and link the implementing change. Keep sensitive,
   open or accepted findings in the controlled
   [risk register](15-risk-register.md); the public record carries only a safe
   identifier or summary.
4. **Fix with a regression proof.** Add a test that fails on the vulnerable
   behaviour and passes on the repair. Run the focused test, its negative
   controls and every affected gate from [09](09-secure-development-lifecycle.md).
   Supply-chain fixes also rerun the relevant dependency or image scan.
5. **Review and close.** Record the fixing commit, commands and exit statuses,
   review result, affected release or image digest, and disclosure decision.
   A penetration-test finding closes only after the assessor or another
   suitably independent tester verifies the repair, or the authorised role
   records a time-bounded acceptance in the controlled risk register.
6. **Coordinate disclosure.** Publish an advisory or release note only after a
   fix or effective mitigation is available, coordinating timing with the
   reporter where possible.
