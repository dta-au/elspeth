# Nyx CI admission and activation

The maintainer selected trusted-workgroup execution on the four existing Nyx
runners. All current organization members are trusted, and the existing outside
repository writer is the maintainer's other account. The requirement is to keep
public, untrusted code off Nyx; separate approval for every trusted commit is not
required. A new execution repository, admission controller or paid selected-
workflow runner-group feature is not part of this policy.

The [prepared workflows](../../config/cicd/prepared-nyx/) remain outside GitHub
discovery. The contributor setting is confirmed, but source activation and native
qualification remain separate operator steps.

## GitHub execution boundary

In **Settings → Actions → General → Approval for running fork pull request
workflows from contributors**, require approval for **all external contributors**
(`approval_policy: all_external_contributors`). A fresh administrator read on
2026-10-11 UTC confirmed:

```json
{"approval_policy":"all_external_contributors"}
```

Leave outsider fork runs unapproved. Review their complete code and workflow
changes before importing accepted work onto a trusted repository branch. Public
users can still fork and open PRs; they cannot push upstream without write access.
Restricting PR submission is unnecessary for this execution policy.

GitHub exempts organization members and repository collaborators from external-
contributor approval, including read-only organization members. Those identities
are explicitly inside the maintainer's trusted workgroup boundary. Review future
membership, writer/admin grants and automation access before extending it. Keep
named access rosters in controlled administration records, not public docs.
[GitHub contributor approval and exceptions](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository#controlling-changes-from-forks-to-workflows-in-public-repositories).

Fork conditions, runner labels, repository tests, read-only token defaults and
action SHA pins do not independently prevent arbitrary code execution. A writer
can edit workflow code and permissions; a fork can remove its YAML conditions.
The external-contributor approval policy and the trusted access population are
the boundary. Never approve outsider runs because a YAML guard appears safe.

Trusted manual starts through `gh` remain authorized. Keep each reviewed manual
workflow's protected-default-branch guard. Reusable calls retain native subject
identity. Privileged events such as `pull_request_target`, issue comments or
`workflow_run` must not fetch and execute outsider code or artifacts. Automatic
CI-triggered image publication remains disabled.

## Required checks and source preparation

Repository ruleset `12348893` is active on the default branch and requires:

- `CI Success`;
- `CodeQL`;
- `Check cohort-attribution trailers on PR commits`; and
- `redaction-gate`.

The 2026-10-11 UTC readback retained strict checks, a PR requirement, zero required
approving reviews and no bypass actors. Required checks have no returned App
source bindings. Preserve the rule and all four runner registrations. Named
status checks assume trusted writers; they do not authenticate workflow content
against a malicious writer inside the accepted trust boundary. Missing checks on
a new commit remain pending. Skipped placeholders and synthetic dispatch results
are not substitutes for native successful PR checks.

The six prepared sources produce none of those checks while parked. Source
contracts inspect both active and prepared inventories and reject duplicates or
accidental discovery. Normal PR execution will retain the actual PR merge SHA,
run ID and attempt; no evidence adapter or `GITHUB_SHA` override is needed.
See [the proof guide](ci-landscape-proof.md) for the unchanged partition, proof and
coverage obligations.

## Remaining qualification and activation

1. Qualify the prepared sources, routing, supply-chain closure, required-success
   aggregation and complete ordinary/proof evidence graph. Source checks do not
   establish native runner, timing or artifact behavior.
2. Read back the contributor setting and trusted access population before
   activation. Reconcile pending jobs and any newly associated automation. The
   read-only inventory confirmed Codex connector access; three other organization
   apps used selected repository scopes whose ELSPETH association was unverified.
   Organization policy and runner-group API reads were unavailable to the current
   credential. These limits are not proof of additional untrusted actors.
3. Obtain explicit activation/publication direction before moving all six sources
   into `.github/workflows` and updating the source-inventory contracts. Publish
   only reviewed source; preserve the current ruleset and four-runner pool.
4. Under an authorized native qualification run, establish that an outsider-
   edited PR workflow stays awaiting approval and acquires no Nyx runner. Leave
   it unapproved. Verify trusted same-repository PR execution, genuine required
   checks, proof/coverage artifacts and rerun/cancellation behavior separately.

Matrix concurrency limits are per matrix, not a global Nyx resource budget.
Coordinate long qualification runs with other host users and preserve all four
runners; no measured resource basis supports retiring two. Host custody and
Docker authority remain governed by the
[runner-hardening runbook](../runbooks/self-hosted-runner-hardening.md).
