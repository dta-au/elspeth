# Security Policy

## Supported Versions

ELSPETH is currently on the `0.8.1` release line. Security
fixes are prioritised against the current release branch and `main`.
Older release snapshots are retained for provenance, but are not
maintained as separately supported long-term release lines.

## Reporting a Vulnerability

Do not open a public GitHub issue containing exploit details, secrets, personal
data, proof-of-compromise material, or instructions that would help a third
party reproduce a vulnerability before maintainers have acknowledged a safe
disclosure path.

Report a vulnerability through either private channel:

1. GitHub private vulnerability reporting for this repository: on the
   **Security** tab, choose **Report a vulnerability**.
2. Email the security contact at
   [cloudengineering@dta.gov.au](mailto:cloudengineering@dta.gov.au).

## What To Include

Include:

- affected version, commit, branch, or container digest;
- affected component or endpoint;
- impact summary;
- reproduction steps or proof of concept;
- whether secrets, personal data, audit records, or external systems were
  exposed;
- logs or audit identifiers, with sensitive values redacted.

## Handling Expectations

Security reports are triaged according to the highest credible impact:

- audit-integrity, secret-disclosure, authentication, authorisation,
  supply-chain, and remote-code-execution issues are release-blocking until
  assessed;
- suspected contract violations in audit, lineage, trust-tier handling,
  redaction, or secret references are treated as security-relevant even when
  they do not resemble a conventional CVE-class vulnerability;
- reports affecting signed judge metadata, trust-tier allowlists, audit-anchor
  integrity, websocket ticket handling, authenticated metrics, provider endpoint
  pinning, bounded execution budgets, or secret-inventory redaction are handled
  as release-gate issues until triaged;
- public disclosure timing is coordinated with the reporter where possible.

## Scope

In scope:

- source code in this repository;
- GitHub Actions workflows and release artifacts;
- the web Composer, authentication, sessions, execution, and audit surfaces;
- trust-tier signing and verification metadata, redaction gates, signed
  allowlists, and CI/CD policy cells;
- official container images and published release bundles;
- documentation that makes security or assurance claims.

Out of scope:

- third-party provider infrastructure such as Azure OpenAI, OpenRouter,
  Microsoft Entra, ChromaDB, Dataverse, and GitHub;
- denial-of-service testing against any live or shared system
  without explicit written authorisation;
- social engineering or physical access attempts.

## Public Release Note

The project is MIT licensed and pre-release. The current product status is in
the [project overview](README.md), and the maintained assurance commitments are
in [Audit and Lineage Guarantees](docs/release/guarantees.md).
