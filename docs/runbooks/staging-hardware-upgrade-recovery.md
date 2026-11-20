# Staging hardware upgrade recovery

Use this runbook to capture a full recovery set of a single-host Docker
deployment before a hardware change, to bring the deployment back afterwards,
and to resume Composer test work on a copied real graph. It was first written
for a staging VM resize during the 0.8.15 hotfix window and was carried into
the repository on 2026-11-20 with the operator tools under `scripts/staging/`.

Every value that names the deployment is a placeholder below:

| Placeholder | Meaning |
| --- | --- |
| `<base-url>` | The deployment origin, `https://...` |
| `<destination>` | An approved backup directory (a mounted backup share or volume) outside the repository |
| `<prefix>` | The artifact prefix below the destination, e.g. `<deployment>/recovery` |
| `<lock-file>` | The lock file the scheduled backup holds while it runs |
| `<postgres-container>`, `<web-container>`, `<gateway-container>` | The running containers |
| `<operator-account>`, `<source-account>` | ELSPETH accounts: the operator's test account and the owner of the source session |
| `<source-session>`, `<copied-session>` | Session ids |
| `<state-dir>` | A private directory outside the repository for the tools' checkpoints and receipts |
| `<checkout>` | The source checkout to preserve, when the host holds one |

The tools print JSON event lines on stdout. Tools that change anything are dry
run by default: run each once without `--execute`, read the plan, then run it
again with `--execute`. Every deployment value is a flag or an environment
variable; a missing one is a usage error (exit 2), never a default.

Keep everything these tools write -- the recovery set, `<state-dir>`
receipts, probe results -- in approved storage. They contain graphs, file
bytes, container environments and identities. `<state-dir>` and
`<destination>` are refused when they are inside the repository.

## Create a fresh recovery set

```bash
python -m scripts.staging.backup_vm_recovery \
  --postgres <postgres-container> \
  --writer <web-container> --writer <gateway-container> \
  --verification-container <web-container> \
  --destination <destination> --prefix <prefix> --lock-file <lock-file> \
  [--source-root <checkout>]
```

Add `--execute` once the plan is right. What it does, in order:

1. Takes `<lock-file>` without waiting; if another backup holds it, it stops.
2. Refuses to continue if any running container carries the judge-metadata
   HMAC key in its environment.
3. Records container, image, network and volume inventories, saves the images
   and, with `--source-root`, archives the checkout and its `.git` directory.
   Each source archive is fingerprinted before and after; a change aborts.
4. Waits until no Composer operation and no run is queued or running on any
   `--postgres` cluster (`--drain-timeout-seconds` turns the wait into a
   failure after that long). It does not cancel work.
5. Pauses the `--writer` containers, re-reads the activity counts and aborts if
   anything changed since the drain check, then captures every database
   (logical dump, cluster globals, physical base backup) and every
   non-database volume. It re-reads the counts again and aborts if the capture
   raced a write. The writers are unpaused whatever happens; a container that
   will not unpause is reported by name.
6. Decodes every logical dump with `pg_restore --list`. For each physical
   backup it measures the archive's rounded file size; when the verification
   volume (the `--verification-mount` volume of `--verification-container`,
   default `/app/state`) has that much free space plus a 128 MiB reserve, it
   extracts a copy, runs `pg_verifybackup`, corrupts `PG_VERSION` and requires
   `pg_verifybackup` to reject it, restores it, verifies again, and removes the
   copy. Otherwise that cluster is recorded as
   `not_run_insufficient_local_space`: its archive's SHA-256 was read back,
   but it was **not** natively verified.
7. Exports each writer's filesystem (its writable layer), archives bind mounts
   and the selected `/etc` paths, writes `RESTORE.txt` and `manifest.json`, and
   only then publishes `<prefix>/CURRENT.json`.

Every artifact is read back from the destination and its SHA-256 compared
before it is recorded. A failed command leaves no partial artifact; its exit
code and stderr are kept beside the set as `failed-command-*.json`. A failed
run writes `failure.json` and never publishes `CURRENT.json`.

## Find the recovery set

Read `<destination>/<prefix>/CURRENT.json`. Use it only when its `status` is
`complete`. It names the immutable set (`prefix`), its `manifest` with SHA-256,
and two lists:

* `physical_verification_passed` -- clusters whose physical archive passed
  `pg_verifybackup` and its corruption negative control;
* `physical_verification_not_run` -- clusters whose physical archive was not
  natively verified for lack of local space. Verify each on a host with room
  (or rely on the logical dumps) before you depend on it.

The manifest lists every artifact with its byte count and hash. `RESTORE.txt`
gives the layout and the reconstruction order. Pin the immutable set; a
`CURRENT.json` pointer alone is not a recovery point.

## Existing VM after resizing or rebooting

Check Docker and the reverse proxy, then the running containers and their
health. The web, gateway, PostgreSQL and network-anchor containers use Docker
restart policies; their mounts, network namespaces, ports and environment are
recorded in the set. Check the public readiness endpoint and an authenticated
session and file read before resuming tests.

If the running containers carry writable-layer hotfixes, compare the installed
package files inside the web container with `src/elspeth/` at the hotfix
commit before resuming.

## Replacement VM

Restore the saved images, networks, volume data, bind files and container
configuration into the approved environment. Each physical archive holds a
whole cluster and its recovery WAL; match it to its saved PostgreSQL image and
ownership. Logical dumps and cluster globals are an alternative path, and the
only verified path for a cluster listed in `physical_verification_not_run`
until its archive has been verified.

The `filesystems/` archives preserve writable-layer hotfixes. Assess them
together with the saved image and container configuration when rebuilding a
container. Configuration and environment artifacts contain secrets and stay
in approved storage.

When the set includes them, extract `source/working-directories.tar.gz` and
`source/repository-git.tar.gz` at the project parent. They hold the git object
database, branches, worktrees and untracked and ignored work, without
rebuildable dependency and cache directories. If the project path changes,
run `git worktree repair` and confirm which tree `elspeth` imports from before
running anything.

## Resume Composer investigation on a copied real graph

The real-graph tools work on a session the operator has already copied into
`<operator-account>`, graph and files together. They do not make the copy:
copy it by a supported means, and record how it was made.

### Re-establish the copy's prompt reviews

A copied graph's file-backed LLM prompts surface as pending prompt reviews in
the copy.

```bash
python -m scripts.staging.repair_copied_prompt_reviews \
  --base-url <base-url> \
  --session <copied-session> --account <operator-account> \
  --source-session <source-session> --source-account <source-account> \
  --state-dir <state-dir>
```

**This exercises approval authority on `<operator-account>`.** With
`--execute` it reads every ready file of the source session and checks each
one's hash and size. Then, for each pending `llm_prompt_template` review, it
proves that the review is live, that it shows the node's current prompt
options, that its bindings are exactly the prompt's file markers, and that
every bound file has the bytes of a source-session file and the text shown
for it. It prints the full text to the terminal, and resolves the review
`accepted_as_drafted` with the reviewed anchor only when the operator types
`approve`. Any other review kind, any mismatch or any other answer stops the
run with that review unresolved.

Use it only for prompts copied unchanged from the source. Never use it for
text authored or edited in the copy: those reviews belong to their reviewer.
The tool goes through `POST .../interpretations/{id}/resolve` and writes
nothing to a database. Afterwards it requires the graph content and chat
history to be unchanged and no review left pending, then runs the normal
`POST .../validate`.

Passwords come from a hidden prompt, or from the variables named by
`--password-env` / `--source-password-env`. A pre-issued bearer token can
be given instead with `--token-env` / `--source-token-env`; a token given that
way is not revoked at the end.

### Run the battery

```bash
python -m scripts.staging.real_graph_battery run-reviewed \
  --base-url <base-url> --session <copied-session> \
  --account <operator-account> --state-dir <state-dir> [--attempt 2] [--cycles 6]
```

`run-reviewed` needs the review repair above to be complete with a passing
validation; `run` skips that requirement and asks the repair turn to fix only
schema declarations and field guarantees. Each run has two phases:

* **repair**: one turn asking the Composer to revalidate the graph without
  changing content (after reviews), or to correct only schema declarations
  and field guarantees (without);
* **edits**: `--cycles` cycles of ten turns that add, swap and remove
  temporary passthrough transforms at the head of the graph and then restore
  the original wiring. The phase starts only when the repair phase is complete
  and the live graph equals its final state, and only after the graph checker
  has rejected two deliberately changed copies of the baseline.

Every turn is an ordinary chat message answered by the configured provider.
After each one the battery requires that only the expected passthroughs
changed, that the existing nodes are unchanged (after a repair turn without
reviews, except `schema` and `required_input_fields`), that the graph is
valid, and that at least one provider call succeeded for that operation. It
records each provider call's latency and flags any call over 300 seconds.
Reading that evidence uses the audit-grade messages view, which records an
access entry each time.

Each turn's operation id is saved before it is submitted, so a resumed run
polls that operation instead of submitting the turn again. A run resumes only
from a checkpoint that is complete and valid for the same session and attempt.
A turn that fails verification stops the attempt for good: inspect the saved
step receipt in `<state-dir>`, diagnose the failure, and start a new
`--attempt`.

For the planned series of small edits used as the release acceptance
measure, see the [Composer edit battery](composer-edit-battery.md).

## Direct Bedrock streaming probe

To check that a direct Bedrock route can stream a single response for longer
than five minutes, without changing the running deployment:

```bash
python -m scripts.staging.direct_bedrock_canary \
  --container <web-container> --region <region> \
  --quick-model <inference-profile> --long-model <inference-profile>
```

With `--execute` it asks for the key with hidden input and sends it once on
the probe's stdin. The key never appears in argv, the environment or output.
It runs inside the container: a short call per model, one long reasoning call
watched for up to `--observe-seconds` (default 660), then a short credential
check. Every request must go to `bedrock-runtime.<region>.amazonaws.com` and a
`/converse-stream` path. Error text has the key removed, and text that still
carries signed-request or key material is withheld. The probe's own stderr is
never relayed. Results go to a private directory below `--result-root` inside
the container. The probe spends provider quota.
