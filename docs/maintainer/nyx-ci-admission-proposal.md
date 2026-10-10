# Operator proposal: exact-commit Nyx admission

**Proposed, unimplemented; activation remains blocked.** The maintainer selected
admission of exact PR commits with enforcement outside contributor-controlled
YAML. The [prepared workflows](../../config/cicd/prepared-nyx/) remain outside
GitHub discovery. Preparing them does not change runner access or required checks.

## Current required checks and inactive behavior

A read-only API query of `dta-au/elspeth` ruleset `12348893` on 2026-10-10 UTC
returned active enforcement on `~DEFAULT_BRANCH`, strict required status checks,
and these contexts, without `integration_id` source bindings:

```json
{"required_status_checks":[{"context":"CI Success"},{"context":"CodeQL"},{"context":"Check cohort-attribution trailers on PR commits"},{"context":"redaction-gate"}],"strict_required_status_checks_policy":true}
```

Preserve those required contexts. While parked, this package publishes none of
them. Missing checks on a new commit remain pending under the rule; conditional
job skips can count as success and are therefore unsuitable as an inactive
placeholder. Existing results at the same SHA, check-producer spoofing and rule
bypasses need external review. GitHub supports binding a required check to a
specific App; external App checks also avoid the event restrictions that prevent
ordinary `workflow_dispatch` checks from fulfilling PR requirements.
[GitHub required-check semantics](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks).

## Proposed enforcement component

Create a private, maintainer-controlled execution repository, proposed name
`dta-au/elspeth-ci-admission`, and an external **Nyx admission controller** with a
GitHub App. ELSPETH contributors have no write or dispatch rights there. The
controller holds approvals, reconciles native runs/jobs, provisions capacity and
publishes the four required checks. Its credentials and management sockets stay
outside all job-reachable filesystems, networks and host-root Docker authority.

Replace direct ELSPETH access to persistent Nyx runners with two controller-owned
disposable VM or equivalent isolated execution slots. Each receives a fresh
single-job runner registration scoped exclusively to the execution repository.
Repository-scoped runners serve one repository; an organization runner group can
instead restrict repository access through settings outside YAML.
[Runner scope](https://docs.github.com/en/actions/concepts/runners/self-hosted-runners),
[runner-group access](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access).

Before creating eligible capacity, the controller must:

1. Authenticate a maintainer approval for the full ELSPETH repository/PR/head SHA,
   observed base, exact executed commit, trusted control revision and suite/profile.
2. Re-read PR state and bind a one-use ticket to the actual execution run,
   attempt, job role and interpreter/shard/proof case. Unknown or ambiguous jobs,
   direct dispatches and reruns receive no capacity.
3. Acquire one of two global slots, then register a fresh runner with a unique
   ticket label. Only trusted execution-repository workflow code can request it.
   Hold the slot until runner revocation and guest teardown are verified.

The external ledger enforces the two-job host cap across every workflow and retry.
Matrix limits alone do not. Extra eligible persistent runners would bypass this
boundary and must be removed from access before activation. A job-start hook,
maintainer label or repository script after runner allocation does not enforce it.

## Supersession and evidence

Approval never transfers to a new SHA. On synchronize, close or revocation, the
controller advances the PR generation, cancels future tickets and suppresses
superseded results. Reconcile queued work after outages. Recommended semantics:
approval authorizes the immutable SHA until revocation is recorded at the
controller's allocation boundary; already allocated work is terminated when
revocation is observed. Webhooks and `read head -> register runner` are not atomic
with a concurrent GitHub push. An absolute ban on old-SHA allocation from the
instant of push needs an additional atomic platform primitive or enforced branch
freeze; this proposal does not claim one exists.

The current same-repository templates cannot simply be copied into the execution
repository. Its native `GITHUB_SHA` identifies control code, while the subject
checkout identifies ELSPETH. A reviewed evidence adapter must bind both repository
identities and commits separately, plus the external admission ticket and native
run/attempt/job. Overriding `GITHUB_SHA` to make the existing comparison pass is
unacceptable. Trusted configuration, actions, harness and consumers come from the
approved control revision; subject code executes only inside the isolated guest.

The controller admits success only after actual native producer success and the
complete ordinary/proof union for both interpreters, finalized hashes, singleton
proof calls and combined 3.13 coverage floors. Failure, skip, cancellation,
omission or missing newest-attempt evidence cannot publish success. CodeQL needs
a separate subject-SHA SARIF upload/processing adapter; trailer and redaction jobs
need explicit subject PR context. These adapters are not implemented. Only the
management-plane App publishes ELSPETH checks, with Checks write permission, after
verification; jobs receive no source-repository check credential.
[Checks API](https://docs.github.com/en/rest/checks/runs#create-a-check-run).

## Minimum operator decision and settings actions

Approve the named execution repository/App, two isolated Nyx slots and the
immutable-SHA revocation semantics before building the controller. Subsequent
operator actions, after implementation review, are:

- In ELSPETH **Settings → Actions → Runners**, remove every Nyx attachment eligible
  for product-repository jobs, including organization/enterprise group access.
  Local service names are not a complete GitHub registration inventory; the prior
  runner API 403 remains unresolved and must not be bypassed.
- Limit execution-repository write, dispatch and administration to the controller
  and trusted operators. Provision only broker-owned, disposable runner capacity;
  audit extra repository/group registrations and direct rerun paths.
- Install the narrowly scoped App with subject read/Checks write and qualified
  CodeQL upload permissions, and execution dispatch/evidence/runner lifecycle
  permissions appropriate to the actual registration model. Keep keys in the
  management plane and validate exact API permissions before enabling it.
- In ELSPETH **Settings → Rules → Rulesets → main** (`12348893`), bind each existing
  required context to the controller App's actual integration ID. Preserve strict
  checks and review applicable bypass actors. Read back the resulting policy.

Before activation, demonstrate that unadmitted commits, contributor-added jobs,
stale tickets, unauthorized dispatch/rerun, duplicate attempts and controller
outages cannot acquire capacity or publish accepted success. Prove credential
isolation, the global slot cap and teardown fencing. Then qualify the real native
artifact, retry, CodeQL and required-check paths, including same-name spoofed
checks. Automatic image publication remains disabled until separately restored.

No repository, App, runner, security setting, deployment or spending change is
performed by this proposal. Until the external component and adapters exist,
continue operator-invoked local qualification and keep automated PR execution
parked. The runner-access finding remains unresolved.
