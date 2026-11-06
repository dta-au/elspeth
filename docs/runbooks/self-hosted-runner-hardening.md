# Self-hosted runner custody and hardening

**Owner:** DTA Cloud Engineering · **Applies to:** the six registered project-owned Nyx
GitHub Actions runners · **Review cadence:** before a release and after runner
installation, reconfiguration or host-access change

This runbook controls the persistent runner host used by ELSPETH project CI.
It does not apply to a deployment operator's own build system. GitHub-hosted
settings, repository workflow admission and host controls are all required:
none substitutes for another.

## 1. Admission boundary

The repository setting **Approval for running fork pull request workflows from
contributors** must be **Require approval for all external contributors**
(`approval_policy: all_external_contributors`). Fork jobs remain ineligible for
the checkout and analysis jobs in the ELSPETH workflows even after approval.
Review workflow-file changes before moving fork code onto a repository branch.

Measure the live setting with an administrator credential:

```bash
gh api repos/dta-au/elspeth/actions/permissions/fork-pr-contributor-approval
```

Expected response:

```json
{"approval_policy":"all_external_contributors"}
```

## 2. Host custody policy

The host has six registered services named
`actions.runner.dta-au-elspeth.nyx-elspeth-{1..6}.service`. Each service runs
as the dedicated `actions-runner` account. DTA Cloud Engineering administers
the host and reviews both interactive host access and membership of the
`docker` group, because Docker access is host-root-equivalent.

The runner directories must be owned by `actions-runner:actions-runner` at
mode `0700`. These runner-owned files must be mode `0600`:

- `.credentials`;
- `.credentials_rsaparams`;
- `.env`; and
- `.path`.

Each service has a systemd drop-in that sets a restrictive umask, removes
ambient and bounding capabilities, prevents privilege gain, gives the service
a private temporary directory and protects system, clock, hostname, kernel and
control-group state. These controls reduce accidental host mutation. They do
not turn a persistent Docker-capable runner into an isolation boundary, so the
admission rule in § 1 remains mandatory.

Docker bind mounts resolve source paths in the daemon's host filesystem.
The runner's private `/tmp` and `/var/tmp` are separate namespaces, so files
created there cannot be supplied to containers by their runner-visible paths.
The PostgreSQL CI job uses `--basetemp` inside its unique checkout for the
TLS fixtures it bind-mounts. Use the same shared-path approach for other
host-runner jobs that supply temporary files to Docker; retain `PrivateTmp`.

### Release CI routing

Keep an open pull request for an active `release/**` branch. Its PR event is
the automatic full-CI producer; release pushes no longer start a second copy
of the matrix. The PR still tests GitHub's synthetic merge commit, and every
required producer must succeed. Main, master and RC pushes retain their
post-merge checks.

CI also declares `workflow_dispatch` for protected main, master, RC and release
refs. Every job checks that admission before acquiring a runner. GitHub's
manual-dispatch availability depends on the workflow being registered on the
default branch; until this change reaches that branch, use the open release
PR. A release branch without a PR does not receive automatic push CI.

Release-head PRs retain the active run when more commits arrive, avoiding
repeated cancellation before any result completes. The pending run can still
be replaced by the newest revision. Other PRs cancel obsolete active runs.

### Shared host capacity: three eligible CI runners

The shared `nyx-ci` pool has **three eligible runners**: `nyx-elspeth-2`,
`nyx-elspeth-3` and `nyx-elspeth-5`. All Nyx workflow jobs must require the
`nyx-ci` label along with their trust/platform labels. GitHub assigns one job
per eligible runner, so this limits the combined pool to three jobs across
workflows and refs, including checkout, dependency installation, browser/image
setup and tests. A matrix's `max-parallel` or a pytest `-n` argument is only a
per-run or per-job bound; it does not reserve shared host capacity.

`nyx-elspeth-1`, `nyx-elspeth-4` and `nyx-elspeth-6` remain registered and
online but have no `nyx-ci` label. Removing that label stops future matching
assignments; already assigned jobs finish normally. During that drain, more
than three jobs can still be active. Do not stop services, unregister runners
or cancel their jobs to enforce the new steady-state pool size.

This is a **job-slot limit, not a memory, CPU or disk-I/O hard cap**. A job can
start multiple workers and Docker containers; local tests and other host
processes do not consume GitHub runner slots. Coordinate those workloads
separately and reduce or defer them when the host is under pressure. More
registered services or spare RAM alone do not authorize more eligible slots.
Future runner bootstrap must omit `nyx-ci` unless the operator intentionally
replaces one of the three eligible runners or approves a revised host budget.
Keep custody hardening on all six services regardless of scheduling eligibility.

Use an administrator's existing GitHub CLI login; never read or print runner
credentials or authentication tokens. Confirm the acting administrator identity
and live inventory:

```bash
gh api user --jq .login
gh api --paginate 'repos/dta-au/elspeth/actions/runners?per_page=100' \
  --jq '.runners[] | {id, name, status, busy, labels: [.labels[].name]}'
```

The following operator commands remove only `nyx-ci` from the three retired
slots, preserving all other labels. The IDs were measured on 2026-11-02;
the name check prevents applying a stale ID to a different runner. Run the
DELETE only when the inventory shows that label is still present:

```bash
set -euo pipefail
test "$(gh api repos/dta-au/elspeth/actions/runners/25 --jq .name)" = nyx-elspeth-1
gh api --method DELETE repos/dta-au/elspeth/actions/runners/25/labels/nyx-ci
test "$(gh api repos/dta-au/elspeth/actions/runners/28 --jq .name)" = nyx-elspeth-4
gh api --method DELETE repos/dta-au/elspeth/actions/runners/28/labels/nyx-ci
test "$(gh api repos/dta-au/elspeth/actions/runners/30 --jq .name)" = nyx-elspeth-6
gh api --method DELETE repos/dta-au/elspeth/actions/runners/30/labels/nyx-ci
```

To restore an approved eligible runner's missing label, take its ID from the
fresh inventory, check its name, then **add** the label rather than replacing
its complete label set. For example, for `nyx-elspeth-2`:

```bash
runner_id=$(gh api 'repos/dta-au/elspeth/actions/runners?per_page=100' \
  --jq '.runners[] | select(.name == "nyx-elspeth-2") | .id')
test -n "$runner_id"
test "$(gh api "repos/dta-au/elspeth/actions/runners/$runner_id" --jq .name)" = nyx-elspeth-2
gh api --method POST "repos/dta-au/elspeth/actions/runners/$runner_id/labels" \
  -f 'labels[]=nyx-ci'
```

After label changes, verify the exact eligible set from GitHub. This check
returns nonzero on a different set; it does not infer eligibility from a local
service count or from jobs that were already running:

```bash
set -euo pipefail
gh api --paginate --slurp 'repos/dta-au/elspeth/actions/runners?per_page=100' | \
  jq -e '[.[].runners[] | select(any(.labels[]; .name == "nyx-ci")) | .name] | sort == ["nyx-elspeth-2", "nyx-elspeth-3", "nyx-elspeth-5"]'
```

## 3. Audit and apply

The audit is read-only and fails closed on a missing runner, custody file,
service or hardening directive:

```bash
sudo scripts/cicd/harden-self-hosted-runners.sh --check
```

Apply the checked policy from a trusted host shell. This requires root, writes
only the six runner directories and six systemd service/drop-in paths,
reloads systemd, restarts each runner sequentially and refuses a partial
layout. Schedule this maintenance after their assigned jobs finish; changing
the scheduling labels alone requires no service restart:

```bash
sudo scripts/cicd/harden-self-hosted-runners.sh --execute
```

Re-run the audit and verify both local and GitHub views:

```bash
sudo scripts/cicd/harden-self-hosted-runners.sh --check
systemctl is-active actions.runner.dta-au-elspeth.nyx-elspeth-{1,2,3,4,5,6}.service
gh api repos/dta-au/elspeth/actions/runners \
  --jq '.runners[] | {name, status, busy, labels: [.labels[].name]}'
```

The audit must end with `runner_host_controls=PASS`; every service must be
`active`, and GitHub must report all six runners `online`. Separately verify
the exact three-runner `nyx-ci` eligible set from § 2. The local hardening
script neither checks nor changes GitHub labels; a passing custody audit
does not authorize restoring all six runners to the scheduling pool.

## 4. Patch and access review

At each review:

1. confirm unattended upgrades are enabled and active;
2. refresh package metadata and apply outstanding security and OS updates;
3. reboot when the package manager or kernel update requires it, then repeat
   the service and GitHub checks from § 3;
4. enumerate interactive accounts, sudo access and Docker-group membership;
5. remove access that is no longer required and record the reviewer, date and
   approved population in the controlled project-administration record; and
6. inspect runner service definitions and `.env` names without copying secret
   values into evidence. The judge-metadata HMAC key must not be present.

Do not publish credential contents, runner registration tokens, hostnames or
named human access rosters in the public assurance pack.

## 5. Incident response

If an unapproved job runs, a runner credential becomes broadly readable, or
host access cannot be reconciled:

1. stop the affected services;
2. remove the affected runners through GitHub's runner administration;
3. rotate their registration credentials by registering clean replacements;
4. investigate the host and invalidate any repository, registry or cloud
   credential that may have been reachable;
5. rebuild the runner host when integrity cannot be demonstrated; and
6. restore service only after this audit and the GitHub inventory both pass.
