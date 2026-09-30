# Runbook: Backup And Recovery

Current as of 2026-10-01.

Back up and restore ELSPETH as a recovery set. Landscape audit data is the
legal record, but a Web deployment also has independent Sessions metadata,
managed blobs, payload objects and, for local authentication, `auth.db`.
Restoring only one member can produce a structurally valid but semantically
inconsistent service.

This runbook supplies safe local SQLite and filesystem mechanics. Managed
PostgreSQL snapshots, object storage, secret stores and cross-service recovery
remain deployment procedures. Record those procedures, owners and tested
recovery points in the controlled deployment copy of the
[incident-response and continuity assessment](../security-assurance/13-incident-response-and-continuity.md).

## Recovery Set

| Component | Runtime location | Restore relationship |
|-----------|------------------|----------------------|
| Landscape | CLI `landscape.url`; Web `landscape_url` or `<data_dir>/runs/audit.db` | Payload references and run evidence must agree with the payload store |
| Payload store | CLI `payload_store.base_path`; Web `payload_store_path` or `<data_dir>/payloads` | Content-addressed bytes referenced by Landscape |
| Sessions | Web `session_db_url` or `<data_dir>/sessions.db` | Blob metadata, identities, sessions and Composer state |
| Managed blobs | `<data_dir>/blobs` | Files whose paths, sizes and hashes are held in Sessions |
| Local credentials | `<data_dir>/auth.db` when local authentication is enabled | Separate credential database; never infer it from Sessions |
| Configuration and keys | Deployment-owned | Required to interpret encrypted databases, signatures and providers |
| Outputs and external stores | Deployment-owned | Include when they are authoritative or cannot be regenerated |

Online backups of separate databases are individually consistent; they are not
an atomic cross-store snapshot. Before a deployment recovery, stop all writers
and record the selected backup identifiers, timestamps, application revision,
configuration revision and the intended recovery point for every member.

For the staging service layout and a detailed open-handle procedure, see
[staging session database recreation](staging-session-db-recreation.md).

## Resolve Active Targets

Do not copy the example paths below into a deployment without first resolving
the live configuration. For a CLI settings file:

```bash
python - <<'PY'
from pathlib import Path
from elspeth.config_loading import load_settings

settings = load_settings(Path("settings.yaml"))
print(f"landscape_url={settings.landscape.url}")
print(f"payload_root={Path(settings.payload_store.base_path).resolve()}")
PY
```

In the running Web environment, `WebSettings.get_landscape_url()`,
`get_session_db_url()`, `get_payload_store_path()` and `data_dir` are the
authorities. Record their sanitized values without printing passwords. Confirm
that each local path is absolute and each database dialect matches the command
you are about to run. Use distinct shell variables such as:

```bash
export LANDSCAPE_DB=/var/lib/elspeth/runs/audit.db
export SESSION_DB=/var/lib/elspeth/sessions.db
export DATA_DIR=/var/lib/elspeth
export PAYLOAD_ROOT=/var/lib/elspeth/payloads
export BLOB_ROOT=/var/lib/elspeth/blobs
```

These are operator inputs, not additional ELSPETH configuration variables.

## SQLite Backup

SQLite's online backup command creates a self-contained database while the
source is running. Back up Landscape and Sessions independently, and back up
`auth.db` when local authentication is enabled:

```bash
set -euo pipefail
BACKUP_DIR=/backups/elspeth/20261001T020000Z
mkdir -m 0700 -p "$BACKUP_DIR"

sqlite3 "$LANDSCAPE_DB" ".backup '$BACKUP_DIR/landscape.db'"
sqlite3 "$SESSION_DB" ".backup '$BACKUP_DIR/sessions.db'"
if [[ -f "$DATA_DIR/auth.db" ]]; then
  sqlite3 "$DATA_DIR/auth.db" ".backup '$BACKUP_DIR/auth.db'"
fi

for backup in "$BACKUP_DIR"/*.db; do
  if ! integrity=$(sqlite3 -readonly "$backup" 'PRAGMA integrity_check;'); then
    rm -f -- "$backup-wal" "$backup-shm" "$backup-journal"
    exit 1
  fi
  # A read-only open of a self-contained backup in WAL journal mode can still
  # leave empty sidecars. They are not members of this backup artefact.
  rm -f -- "$backup-wal" "$backup-shm" "$backup-journal"
  [[ "$integrity" == ok ]]
done
```

If Landscape uses SQLCipher, use the deployment's approved SQLCipher-aware
backup command with the correct key. A plain `sqlite3` success is not evidence
that an encrypted database was backed up. Never print the key or place it in
the backup directory.

## Payload And Managed-Blob Backup

Archive the resolved roots, not hard-coded `data/` paths. Record a relative
file manifest and archive digest for each root. The following function stores
only the root basename, so restore can target any approved parent directory:

```bash
archive_tree() {
  local root=$1 archive=$2 manifest=$3 parent name archive_parent archive_name
  if [[ ! -d "$root" ]]; then
    printf 'tree root does not exist: %s\n' "$root" >&2
    return 1
  fi
  parent=$(dirname -- "$root")
  name=$(basename -- "$root")
  if ! (
    set -o pipefail
    cd "$root"
    find . -type f -print0 | sort -z | xargs -0 -r sha256sum
  ) >"$manifest"; then
    return 1
  fi
  if ! tar -C "$parent" -czf "$archive" -- "$name"; then
    return 1
  fi
  archive_parent=$(dirname -- "$archive")
  archive_name=$(basename -- "$archive")
  if ! (cd "$archive_parent" && sha256sum "$archive_name" >"$archive_name.sha256"); then
    return 1
  fi
}

archive_tree "$PAYLOAD_ROOT" "$BACKUP_DIR/payloads.tar.gz" "$BACKUP_DIR/payloads.manifest.sha256"
archive_tree "$BLOB_ROOT" "$BACKUP_DIR/blobs.tar.gz" "$BACKUP_DIR/blobs.manifest.sha256"
```

Copy settings and non-secret configuration separately. Store secret-bearing
environment files and database keys only in their restricted secret-management
system.

## PostgreSQL Backup And Restore Boundary

A Web deployment can have separate Landscape and Sessions PostgreSQL URLs.
Dump both resolved URLs; `$ELSPETH_DATABASE_URL` is not a substitute for this
two-database inventory:

```bash
pg_dump --format=custom --dbname="$LANDSCAPE_DATABASE_URL" --file="$BACKUP_DIR/landscape.dump"
pg_dump --format=custom --dbname="$SESSION_DATABASE_URL" --file="$BACKUP_DIR/sessions.dump"
```

Use managed-service snapshots or WAL/PITR when they are the deployment's
recovery authority. Restore logical dumps only into freshly prepared targets,
using `pg_restore --exit-on-error`, then restore ownership and runtime grants.
Select and record a coherent recovery point for both databases and their
filesystem/object-store members. PostgreSQL recovery is incomplete until the
deployment-specific procedure proves both targets and their cross-store data;
this generic runbook cannot choose provider snapshots, roles or encryption
keys.

## Restore SQLite Safely

Stop every application, worker and maintenance process that can write any
member of the recovery set. Verify no process holds the target database or its
sidecars open. Treat `<db>`, `<db>-wal`, `<db>-shm` and `<db>-journal` as one
artifact set: committed rows can exist only in the WAL, and a stale WAL can be
validly replayed into an older copied main file.

The following function preserves the complete old artifact set, verifies the
selected self-contained backup in a fresh path and publishes it only after the
old main file and sidecars have been removed from the target namespace:

```bash
restore_sqlite() {
  local target=$1 backup=$2 label=$3 quarantine=$4 staged suffix integrity
  if [[ ! -f "$backup" ]]; then
    printf 'SQLite backup does not exist: %s\n' "$backup" >&2
    return 1
  fi
  if ! mkdir -m 0700 -p "$quarantine"; then
    return 1
  fi
  # Refuse reuse of a recovery ID/label: mixing a later main file with the
  # first attempt's WAL/SHM would destroy the evidentiary artifact set.
  if ! mkdir -m 0700 "$quarantine/$label"; then
    printf 'quarantine label already exists; choose a new recovery ID: %s\n' "$quarantine/$label" >&2
    return 1
  fi

  for suffix in '' -wal -shm -journal; do
    if [[ -e "${target}${suffix}" ]]; then
      if ! mv -- "${target}${suffix}" "$quarantine/$label/"; then
        return 1
      fi
    fi
  done

  staged="${target}.restore.$$"
  if ! install -m 0600 -- "$backup" "$staged"; then
    return 1
  fi
  if ! integrity=$(sqlite3 -readonly "$staged" 'PRAGMA integrity_check;'); then
    rm -f -- "$staged" "$staged-wal" "$staged-shm" "$staged-journal"
    return 1
  fi
  # A self-contained backup can retain WAL journal mode. Even a read-only
  # integrity check may then create empty sidecars beside the staged file.
  rm -f -- "$staged-wal" "$staged-shm" "$staged-journal"
  if [[ "$integrity" != ok ]]; then
    rm -f -- "$staged"
    return 1
  fi
  if ! sync -f "$staged"; then
    rm -f -- "$staged"
    return 1
  fi
  if ! mv -- "$staged" "$target"; then
    return 1
  fi
  sync -f "$(dirname -- "$target")"
}

RECOVERY_ID=20261001T041500Z
QUARANTINE="$DATA_DIR/recovery-quarantine/$RECOVERY_ID"
restore_sqlite "$LANDSCAPE_DB" "$BACKUP_DIR/landscape.db" landscape "$QUARANTINE"
restore_sqlite "$SESSION_DB" "$BACKUP_DIR/sessions.db" sessions "$QUARANTINE"
if [[ -f "$BACKUP_DIR/auth.db" ]]; then
  restore_sqlite "$DATA_DIR/auth.db" "$BACKUP_DIR/auth.db" auth "$QUARANTINE"
fi
```

Restore the deployment's required owner/group and final file mode before
starting a service. Keep the quarantine directory until recovery acceptance;
it is incident evidence and includes any pre-restore WAL-only state.

## Restore File Trees Safely

Do not extract an unexamined archive into `/` or over a live store. Verify the
archive digest, reject absolute paths, traversal, links and device entries,
extract into a staging directory on the destination filesystem, then verify
the manifest:

```bash
stage_tree() {
  local archive=$1 manifest=$2 destination=$3 stage parent name first_file archive_parent archive_name
  archive_parent=$(dirname -- "$archive")
  archive_name=$(basename -- "$archive")
  if ! (cd "$archive_parent" && sha256sum -c "$archive_name.sha256" >&2); then
    return 1
  fi
  parent=$(dirname -- "$destination")
  name=$(basename -- "$destination")
  if [[ ! -d "$parent" ]]; then
    printf 'destination parent does not exist: %s\n' "$parent" >&2
    return 1
  fi
  if ! stage=$(mktemp -d "$parent/.${name}.restore.XXXXXX"); then
    return 1
  fi

  if ! python - "$archive" "$name" <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

expected_root = sys.argv[2]
with tarfile.open(sys.argv[1], "r:gz") as source:
    for member in source.getmembers():
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] != expected_root
        ):
            raise SystemExit(f"unsafe archive path: {member.name!r}")
        if member.issym() or member.islnk() or member.isdev():
            raise SystemExit(f"unsafe archive member type: {member.name!r}")
PY
  then
    rm -rf -- "$stage"
    return 1
  fi

  if ! tar -C "$stage" --no-same-owner --no-same-permissions -xzf "$archive"; then
    rm -rf -- "$stage"
    return 1
  fi
  if [[ ! -d "$stage/$name" ]]; then
    rm -rf -- "$stage"
    return 1
  fi
  if [[ -s "$manifest" ]]; then
    if ! (cd "$stage/$name" && sha256sum -c "$manifest" >&2); then
      rm -rf -- "$stage"
      return 1
    fi
  else
    if ! first_file=$(find "$stage/$name" -type f -print -quit); then
      rm -rf -- "$stage"
      return 1
    fi
    if [[ -n "$first_file" ]]; then
      printf 'empty manifest does not match non-empty tree\n' >&2
      rm -rf -- "$stage"
      return 1
    fi
  fi
  printf '%s\n' "$stage/$name"
}

STAGED_PAYLOADS=$(stage_tree "$BACKUP_DIR/payloads.tar.gz" "$BACKUP_DIR/payloads.manifest.sha256" "$PAYLOAD_ROOT")
STAGED_BLOBS=$(stage_tree "$BACKUP_DIR/blobs.tar.gz" "$BACKUP_DIR/blobs.manifest.sha256" "$BLOB_ROOT")
```

After both staged trees pass, move the old roots into the recovery quarantine
and rename the staged roots into the exact resolved destinations. Use one
filesystem per rename so publication is atomic. Restore deployment ownership
and modes, fsync both parent directories, and do not merge a restored tree with
unverified current files.

## Recovery Validation

Keep writers stopped until every applicable check succeeds:

1. Run native integrity and schema/version checks against Landscape, Sessions
   and `auth.db` where applicable.
2. Compare the selected recovery manifest's row counts and stable identifiers
   with the restored databases. A raw count without an accepted pre-restore
   reference does not prove the recovery point.
3. Run `elspeth explain --run <RUN_ID> --database "$LANDSCAPE_DB" --json` for
   selected recovered runs.
4. Retrieve and re-hash referenced payloads through
   `FilesystemPayloadStore`; the AWS deployment's maintained equivalent is the
   `verify-payloads` acceptance command. Missing payloads are recovery loss,
   not a successful restore with a harmless warning.
5. Through an authorised session/service path, retrieve representative ready
   managed blobs and prove their stored paths, sizes and content hashes agree
   with Sessions. ELSPETH does not currently ship a comprehensive offline
   all-blob verifier; record that limitation or supply a deployment-owned
   enumerating check before claiming complete blob validation.
6. Test local authentication and administrative recovery when `auth.db` was
   restored.
7. Start the service without user traffic, run `/api/ready`, then exercise a
   representative authenticated workflow before reopening ingress.

`/api/ready` proves database connectivity/schema and directory writability. It
does not enumerate existing payloads or blobs, re-hash restored content, or
prove a cross-store recovery point.

If the restore is for an interrupted CLI run, inspect the dry-run result before
execution:

```bash
elspeth resume <RUN_ID> --database "$LANDSCAPE_DB"
elspeth resume <RUN_ID> --database "$LANDSCAPE_DB" --execute
```

Never patch or coerce Tier-1 audit rows to make a recovery check pass. Preserve
the failed recovery set and select another known-good point.

## Retention

Set backup and quarantine retention from deployment policy, legal requirements
and tested recovery objectives. The default payload purge-eligibility setting
is 90 days; it does not set audit-database or backup retention and does not
delete content automatically.

## See Also

- [Database Maintenance](database-maintenance.md)
- [Incident Response](incident-response.md)
- [Resume Failed Run](resume-failed-run.md)
- [Configuration Reference](../reference/configuration.md)
