# VM live-container Composer credential-limit hotfix

Status: proposed execution procedure, dated 2026-11-20. This document does not
implement a patch, restart a service, or authorize a deployment, and no release
assignment is implied.

**Reconciliation with 0.8.16.** The two-file credential-limit patch this
procedure was written for was not implemented as planned. 0.8.16 instead raises
the physical-boundary credential scan ceiling to 2 MiB in
`src/elspeth/web/composer/provider_gateway.py` (commit `ddf6cf234`), and that
ships in the image. No writable-layer install is needed for it. Keep this
document as the reviewed method for installing a small, tested Python change
into a stopped container's writable layer. Before any new use, re-identify the
target files and re-check this procedure's prerequisites for them. The
procedure's design was reviewed and approved; its measured execution
prerequisites below remain mandatory before a live rollout.

Use this procedure for an existing Docker deployment on a VM where the model
gateway joins the web container's network namespace. Install the tested two-file
Python change into the existing container's writable layer during a controlled
availability interruption. Database backups are a recovery precaution; normal
rollback restores code while retaining current database contents.

## Inputs and evidence locations

Resolve these inputs immediately before execution; do not paste example paths
or old container identifiers into a live command:

| Input | Required value |
| --- | --- |
| `HOTFIX_REPO`, `HOTFIX_WORKTREE` | Project root and dedicated implementation worktree |
| `HOTFIX_WEB`, `HOTFIX_GATEWAY` | Existing web and model-gateway container names |
| `HOTFIX_WEB_ID`, `HOTFIX_IMAGE` | Full current web container ID and image digest |
| `HOTFIX_GATEWAY_ID` | Full current gateway container ID |
| `HOTFIX_RECORDS_DIR` | New private operator-task directory under approved storage's resolved persistent data directory |
| `HOTFIX_ARTIFACTS` | Code-only package directory inside the worktree |
| `HOTFIX_TEST_RUN` | New lane-private directory inside the worktree for synthetic test logs and temporary files |
| `HOTFIX_CANDIDATE_TAR`, `HOTFIX_ROLLBACK_TAR` | Reviewed candidate and exact baseline code archives |
| `HOTFIX_STOP_SECONDS` | Grace interval chosen from live shutdown budgets, with a margin |
| Backup reference | Exact immutable recovery-set manifest (see [staging hardware upgrade recovery](staging-hardware-upgrade-recovery.md)) |

Validate nonempty inputs and resolved paths before any command. Test directories
must be owned by this run and inside the worktree. Pytest may remove its basetemp
contents; never point it at source, staging state or another run's directory.

Keep staging data, runtime telemetry, private deployment records, canary results,
and backup manifests in approved storage or the designated backup storage account.
Code, synthetic tests and code-only packages can remain in the project/worktree.
Never save full Docker inspect output, credentials, request bodies, or service
logs to workspace files. Read authentication configuration in memory; do not
print tokens or include them in shell arguments. Use no `/tmp` locations.

The database recovery set contains individually consistent online snapshots,
not an atomic cross-store snapshot. It does not include managed blobs, payload
files or secret-bearing configuration. Preserve those stores and keys; a future
database restore is a separate controlled recovery operation.

## 1. Prepare and prove the patch before the interruption

1. Create a dedicated worktree from the agreed deployment-compatible commit.
   Read CONTRIBUTING's whole-tree requirements before writing code. Implement
   the two production files and the regressions in the approved plan. Change
   neither global scanner policy nor persisted graph/history data.
2. Run the focused credential, Advisor, physical-provider, prompt-guard, custody,
   audit and whole-tree inventory checks. Include credentials beyond the old
   limit, exact original-wire fidelity, supported non-string content, byte
   boundaries, invalid Unicode, cycles and ordinary residual limits.
3. Run the required default full Python suite and gates with one worker on this
   shared host after checking capacity. Use a test-environment allowlist without
   staging database URLs, credentials, state paths, cloud/live-test selectors or
   inherited provider credentials. Check test fixtures and subprocess/default
   temporary paths before running. Use both worktree import roots and explicit
   project-local log/temp paths; the gate's default log location is outside the
   project. In the sanitized test environment, run the canonical detached script:

   ```bash
   cd "$HOTFIX_WORKTREE" && \
     mkdir -p "$HOTFIX_TEST_RUN/logs" "$HOTFIX_TEST_RUN/tmp"
   cd "$HOTFIX_WORKTREE" && TMPDIR="$HOTFIX_TEST_RUN/tmp" \
     scripts/full-suite-gate.sh --execute --detach --root "$HOTFIX_WORKTREE" \
     --log-dir "$HOTFIX_TEST_RUN/logs" \
     --pytest-args "--basetemp '$HOTFIX_TEST_RUN/pytest-temp'" \
     --workers 1 --stages ruff,mypy,contracts,lints,pytest
   ```

   Wait for the recorded completion marker. Read all stage exit codes and the
   frozen-tree result; do not infer success from a running process or partial
   output. Inspect lints separately: this gate treats them as nonfatal by default.
   Record any keyless lint baseline/drift and remaining operator-signing
   obligation accurately. No PostgreSQL/schema/lock edits are planned.
4. Review and commit the exact patch using the branch-safety preflight and
   explicit owned pathspecs. Never bypass hooks. Retain the commit and test
   provenance in the code review. Do not merge or deploy a different tree after
   testing without assessing the difference.

## 2. Resolve the installed files and prepare rollback

1. Inspect container IDs, running state, image digest, mounts, restart policy,
   process command and network mode without writing credential-bearing inspect
   output to disk. Confirm the gateway's network mode is `container:` followed
   by the current full web ID. Resolve module paths with the running interpreter
   using `importlib.util.find_spec()`; do not assume `/app/src` is imported.
   Check root/mount writability and resolve the files and their parent paths:
   both targets must be ordinary non-symlink source files in the writable layer,
   not bind/read-only mounts or an alternate import tree. Prove the configured
   runtime user can read them. Record both full container IDs and use these IDs,
   not mutable names, for every subsequent stop/start/copy.
2. Measure bytes/hashes and uid/gid/mode for:
   - `elspeth.web.composer.provider_gateway`;
   - `elspeth.web.credential_guard`;
   - relevant scanner, bounded-JSON, Advisor and prompt/provenance dependencies.

   Compare these with the tested baseline. Refuse a blind overwrite if the live
   baseline differs; account for the actual deployed changes first. Image labels
   alone do not capture previous changes to a container's writable layer.
3. Copy the exact original two module files and a small sanitized deployment
   record into `HOTFIX_RECORDS_DIR`. Record the concrete backup manifest reference,
   module paths, hashes, ownership/modes, interpreter version, container/image
   IDs and current network binding. Verify the stored originals against the live
   bytes. Do not export the environment or secret mounts into this record.
   Confirm this directory is inside the actual persistent approved-storage mount, with
   private permissions and application-appropriate ownership. A similarly named
   host directory is not an authorized data destination. Stream runtime readbacks
   and records into this destination or backup storage without workspace copies.
4. Create code-only candidate and rollback archives using the resolved installed
   paths relative to `/`. They must contain exactly the two regular module
   files: no links, absolute names, traversal, directories, bytecode, dependency
   installations, frontend assets or state. The rollback bytes must equal the
   verified originals. Preserve recorded uid/gid/mode.
   Use plain USTAR regular-file entries; reject duplicate members, extension
   headers and effective path/link overrides as well as forbidden names/types.
5. Identify the deployed interpreter/loader, optimization mode, cache prefix and
   relevant `.pyc` locations/headers, including hash-check options. Absence of
   caches and normal timestamp caches permit this source-only procedure. Give
   each publication a fresh whole-second mtime distinct from current source,
   any relevant timestamp-cache header and previous publication. Checked hash
   caches require proof that source-hash validation is enabled. Refuse unchecked
   or deliberately skipped hash caches; targeted bytecode repair would require
   a separate procedure amendment. Exercise candidate and rollback cached imports
   with same-size synthetic code under the actual Python configuration. Do not
   clear or precompile caches to make this control pass. `-B` and
   `PYTHONDONTWRITEBYTECODE` prevent writes; neither disables cache reads.
6. Prove the archive install, exact two-file readback, ownership/modes, fresh
   import, and rollback sequence in a disposable container using the same image
   and Python version. Override its application entrypoint; publish no ports,
   attach no staging state, and provide no production secrets or environment.
   Compile/import the patched modules without starting ELSPETH or touching live
   databases. The image may predate writable-layer edits: inspect dependency
   differences and use a bounded code-only overlay if needed, not a state copy.
   Run the focused behavioral regressions under that native interpreter and
   installed dependency set, proving the candidate module paths/hashes. Prepare
   frozen test dependencies in an isolated derivative if the runtime image lacks
   pytest; never install into the running service's venv. Keep test execution
   network-disabled and synthetic, with all temporary/log files under the test
   worktree. Override pytest's configured `pythonpath` and exported `PYTHONPATH`
   for this native run so the application imports the patched installed package,
   not the worktree source or a development editable install; resolve the lints
   source root separately and check spawned-interpreter origins too.
   Import/compile success alone is not behavioral validation.
7. Verify the exact immutable database backup manifest, all required members,
   hashes and successful readback. Reconcile its inventory with the active stores.
   Use the existing backup destination and credential mechanism. A `CURRENT.json`
   pointer alone is not enough; pin the immutable set. A set captured by
   `scripts/staging/backup_vm_recovery.py` lists, in its pointer, the clusters whose
   physical archive was NOT natively verified for lack of local space; verify those
   before relying on them. Do not silently substitute
   or overwrite the selected recovery point.
   Record the cron mechanism, account/container, immutable set creation time and
   application provenance. Require native complete PostgreSQL archive decoding
   and SQLite integrity checks, or the approved SQLCipher-aware equivalent when
   encrypted. Remote hashes alone do not prove native database integrity. Keep
   this prerequisite bounded to the selected database backup.
8. Present the exact tested commit, two target paths, archive hashes, baseline
   hashes, checks and rollback commands for the separately authorized live
   window. Have the restoration package ready before stopping anything.

## 3. Quiet and stop the existing services

1. Before the window, record the exact existing ingress close/reopen commands,
   operator/probe access, and treatment of established HTTP/WebSocket connections.
   Use the agreed maintenance mechanism to block new user work; let established
   operations finish. Leave the gateway available while work drains. Pause or
   coordinate deployment/autoheal activity through its existing controls so it
   cannot replace/restart either container during publication; do not silently
   alter restart policy. One operator owns the window.
2. Require zero queued/running rows in `composer_async_operations`, zero
   in-progress `session_operation_receipts`, no unresolved work-owning
   `session_operation_fences`, and no queued/running execution work. Reconcile
   persisted running records in the active Sessions/Landscape stores with actual
   executor ownership and fences: historical interrupted records must not be
   edited or treated as proof of current work. Resolve schemas and database-clock lease
   semantics through their read-only authorities; expired leases do not by
   themselves prove a worker stopped. Account for existing connections and
   queued background work. Do not create a new service object and treat its empty
   in-memory registry as evidence about the running app. If work is outstanding,
   wait or arrange a separately approved cancellation; do not delete leases or
   infer idleness from CPU. Keep count/status evidence in approved storage.
3. Recheck both container IDs, exact baseline file hashes and network binding
   immediately before stopping. If another deployment has changed them, stop
   this procedure and reconcile; do not patch by a reused container name.
4. Resolve the process command/stop signal and shutdown budgets. Gracefully stop
   the web container first, then the gateway. This lets web
   shutdown retain access to providers while it drains:

   ```bash
   cd "$HOTFIX_REPO" && \
     sudo -n docker stop --timeout "$HOTFIX_STOP_SECONDS" "$HOTFIX_WEB_ID"
   ```

   Require stopped state, no OOM/forced kill, an acceptable exit status and
   application shutdown-completion evidence for this stop. Capture the bounded
   shutdown interval in approved storage; require completed application/server
   shutdown and no lifecycle/finalizer failure. Correlate membership stop and
   operation settlement where applicable. Docker stop success or exit status
   alone is insufficient: `app.py` raises accumulated finalizer failures.
   A timeout/forced kill is not success: investigate and assess
   outstanding work before modifying code. Then stop and verify the gateway:

   ```bash
   cd "$HOTFIX_REPO" && \
     sudo -n docker stop --timeout "$HOTFIX_STOP_SECONDS" "$HOTFIX_GATEWAY_ID"
   ```

   Operate on these recorded two containers only. Keep the existing container
   objects, volumes, environment, image configuration and namespace reference.

## 4. Install the two-file package while stopped

1. Confirm both recorded containers remain stopped and the candidate archive's
   allowlist, content hashes and metadata still match the reviewed package.
2. Extract the validated code-only archive into the stopped web container:

   ```bash
   cd "$HOTFIX_REPO" && \
     sudo -n docker cp --archive - "$HOTFIX_WEB_ID:/" < "$HOTFIX_CANDIDATE_TAR"
   ```

   This pair of file replacements is not atomic. Keeping the app stopped is the
   publication boundary. A partial copy means restore both original files before
   startup; never start with one old and one new module.
3. Read each installed file back using Docker's archive interface, which works
   while the container is stopped. Validate exact hashes, uid/gid/mode and fresh
   source mtimes. Store runtime verification records in the approved storage or backup path;
   only pure code bytes may be inspected in the worktree. Do not run `docker exec`
   against a stopped container or start it solely to discover a bad copy.

## 5. Start, verify and reopen

1. Start the existing web container, then immediately start the existing gateway:

   ```bash
   cd "$HOTFIX_REPO" && sudo -n docker start "$HOTFIX_WEB_ID"
   cd "$HOTFIX_REPO" && sudo -n docker start "$HOTFIX_GATEWAY_ID"
   ```

   Do not wait for web readiness before starting the gateway: configured startup
   model probes may need it. The gateway must join the web's current namespace.
   Starting it before the web, or leaving it running in an old namespace, can
   make Composer unavailable despite an apparently running gateway.
   Check each start result and observed running state; if either fails, stop the
   candidate sequence and enter bounded rollback with ingress still closed.
2. Within a bounded startup deadline, prove both recorded containers are running,
   the gateway binding still names the exact web ID, and the model gateway's
   authenticated `/readyz` probe succeeds from the web namespace with the
   configured model aliases. Read its bearer credential from the web environment
   inside the container; save sanitized results only in approved storage.
   Resolve the `/readyz` URL from the existing provider endpoint, send its existing
   bearer header, and require the configured JSON `ready`/`model_aliases` contract.
   Record boot-probe outcomes accurately: starting promptly reduces the gateway
   race, but transient/unqualified probes can coexist with successful app boot.
3. Require HTTP 200 from application `/api/health` and `/api/ready` locally and
   through the controlled operator/probe route on the existing public origin
   while ordinary users remain blocked. If the maintenance mechanism blocks
   health paths too, arrange that probe access before the window. Check installed
   hashes and inspect fresh
   startup/audit failures without exporting raw logs. Readiness proves service
   connectivity; it does not prove the former credential-limit defect is fixed.
4. Run an authorized canary through the normal Composer path in an agreed session,
   with graph-changing scope specified beforehand. Use a representative large
   composite graph context, verify real provider dispatch, accurate resource
   refusal behavior, and unchanged wire/credential protections. Keep prompts,
   request context and results in approved storage. Synthetic credential controls belong
   in isolated tests; never use a real secret as a live canary.
5. Reopen ingress only after service readiness and the agreed canary pass. Record
   the installed patch commit, module hashes, backup reference and acceptance
   result in the controlled approved-storage record. If acceptance is incomplete, keep the
   window open for rollback rather than declaring the hotfix successful.

## 6. Code rollback

Rollback on a failed install, startup/readiness failure, Composer regression,
credential-protection failure or unexplained live error.

1. Keep ingress closed. If candidate processes started, gracefully stop the web
   before the gateway, using the same clean-stop checks. If either process cannot
   stop cleanly, assess the outstanding work before further action.
2. Recheck exact container identities. Publish a rollback archive with the exact
   original bytes and ownership/modes, and a fresh source mtime to invalidate
   candidate bytecode:

   ```bash
   cd "$HOTFIX_REPO" && \
     sudo -n docker cp --archive - "$HOTFIX_WEB_ID:/" < "$HOTFIX_ROLLBACK_TAR"
   ```

3. Verify both restored files through the stopped-container archive readback.
   Start web, immediately start gateway, then repeat readiness and a bounded
   normal Composer check. Reopen ingress only when the previous behavior is
   established. Retain failure evidence in the approved storage or backup destination.
4. Do not restore databases, delete state, recreate containers, rerender secrets
   or run schema initialization as part of code rollback. Any recovery requiring
   those actions needs a separate assessment and operator scope.

## Follow-through

The installed hotfix survives stopping/starting these existing containers. It
does not change their base image and will disappear if the web container is
recreated from that image. Keep the committed patch and controlled deployment
record so the next separately approved image build carries it forward. Preserve
other known writable-layer changes when preparing that later build.
