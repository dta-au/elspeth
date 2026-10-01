# Self-hosted runner custody and hardening

**Owner:** DTA Cloud Engineering · **Applies to:** the four project-owned Nyx
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

The runner pool has four services named
`actions.runner.dta-au-elspeth.nyx-elspeth-{1..4}.service`. Each service runs
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

## 3. Audit and apply

The audit is read-only and fails closed on a missing runner, custody file,
service or hardening directive:

```bash
sudo scripts/cicd/harden-self-hosted-runners.sh --check
```

Apply the checked policy from a trusted host shell. This requires root, writes
only the four runner directories and four systemd service/drop-in paths,
reloads systemd, restarts each runner sequentially and refuses a partial
layout:

```bash
sudo scripts/cicd/harden-self-hosted-runners.sh --execute
```

Re-run the audit and verify both local and GitHub views:

```bash
sudo scripts/cicd/harden-self-hosted-runners.sh --check
systemctl is-active actions.runner.dta-au-elspeth.nyx-elspeth-{1,2,3,4}.service
gh api repos/dta-au/elspeth/actions/runners \
  --jq '.runners[] | {name, status, busy, labels: [.labels[].name]}'
```

The audit must end with `runner_host_controls=PASS`; every service must be
`active`, and GitHub must report all four runners `online` before CI capacity
is restored.

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
