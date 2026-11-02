# Reviewed CI checkout cleanup

The Nyx runner host already owns `/usr/local/sbin/runner-workspace-cleanup`.
Use that implementation for stale CI checkouts. Do not substitute a blanket
Docker prune or recursive deletion of a runner workspace.

The repository wrapper, `scripts/cicd/cleanup_completed_ci.py`, restricts a
reviewed invocation to explicit GitHub run IDs and one deletion worker. It
verifies the installed helper's exact reviewed SHA-256 before loading it.
It preserves the helper's process-reference, container-bind, mount, inode,
listener-guardian and quarantine protections. A changed helper is refused
until its source has been reviewed and the wrapper's binding updated.

## Select completed runs

Confirm the repository and inspect each candidate run through GitHub:

```bash
gh run view RUN_ID --repo dta-au/elspeth \
  --json databaseId,status,conclusion,headSha
```

Select only terminal runs whose disposable CI checkouts are no longer
needed. Cancellation is a terminal state, not a successful test result.
Retain the GitHub logs/artifacts needed for diagnosis; deleting a local
checkout does not establish or replace release qualification.

Run the wrapper as root **in the host mount and PID namespaces**. Container
visibility limited to its own processes is insufficient to establish that
a host checkout is unused. An unreadable process inventory must fail; do
not interpret it as an empty inventory.

From the project root, dry-run one or more explicit IDs:

```bash
sudo python3 scripts/cicd/cleanup_completed_ci.py \
  --run-id RUN_ID --run-id ANOTHER_RUN_ID
```

The default is dry-run. Review the JSON decisions. `protected` means the
checkout stays, including when a stopped container still binds it. Never
remove that container merely to make this cleanup succeed. The minimum
checkout age remains one hour.

## Execute and verify

After reviewing the exact candidates, use the same IDs:

```bash
sudo ionice -c3 nice -n19 python3 scripts/cicd/cleanup_completed_ci.py \
  --execute --run-id RUN_ID --run-id ANOTHER_RUN_ID
```

Both newly discovered candidates and interrupted quarantine entries are
restricted to the supplied IDs. The installed helper refreshes its live
checks before quarantine and before removal. Its listener-pause guardian
has a three-second lease; a job transition or exhausted lease defers work
to a later invocation. Retry the same reviewed IDs for those deferrals.

Capture the exit status, JSON decisions, stderr and filesystem free space
before/after. Check that listeners resumed and no owned cleanup processes
remain. An interrupted run may leave quarantined data; use the same helper
and allowlist to finish it. Do not delete quarantine directories by hand.
The installed thread-pool cleanup waits for queued deletion work while
unwinding SIGTERM/SIGHUP, so a graceful signal is not an immediate hard
wall-clock bound. External timeout escalation can interrupt removal and
requires the same subsequent state checks.

The host's `runner-cleanup.timer` is a separate operational policy. A manual
reviewed cleanup does not authorize removing a dated timer pause or changing
its schedule. Keep explicit pauses until their stated expiry or a separate
operator decision.

## Bound device traffic for large cleanups

The November2 cleanup showed that serial deletion with `ionice -c3` and
`nice -n19` could still produce substantial I/O pressure. Nyx's NVMe uses
the `none` scheduler. For a large cleanup, use an actual device bandwidth
limit in addition to low priority, and verify the kernel accepted it.

The reviewed administrative Docker invocation used these options on its
own ephemeral container, before entering the host mount/PID namespaces:

```text
--device-read-bps /dev/nvme0n1:10mb
--device-write-bps /dev/nvme0n1:10mb
```

Its actual cgroup `io.max` read:

```text
259:0 rbps=10485760 wbps=10485760 riops=max wiops=max
```

Confirm the device identity and cgroup path on the current host. Keep the
cleanup process in its limited cgroup when entering the host mount/PID
namespaces, and verify its actual membership. Do not move it out of that
resource group. The measured retry charged read/write I/O to that cgroup
and completed under the configured limits. This does not prove every
filesystem journal or metadata operation is attributed to the cleanup
cgroup. Monitor host pressure and let it settle before another batch.
