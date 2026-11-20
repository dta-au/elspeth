"""Capture a full recovery set of a single-host Docker deployment of ELSPETH.

The set holds container, image, network and volume inventories; saved images;
every PostgreSQL database (logical dumps, cluster globals and a physical base
backup); non-database volumes; writable-layer filesystem exports of the
application containers; bind-mounted configuration; selected host
configuration; and, optionally, the source checkout with its git object store.

Safety properties (each is named by a test in
``tests/unit/scripts/staging/test_backup_vm_recovery.py``):

* The run holds an existing lock file (normally the scheduled backup's lock),
  so it never overlaps another backup.
* It refuses to start if any running container carries the judge-metadata
  HMAC key in its environment, so that key is never copied into a backup.
* Before pausing application writers it WAITS until no Composer operation and
  no run is queued or running on any cluster. After pausing it re-reads the
  activity counts and aborts if anything changed since the drain check; it
  re-reads them again after capture and aborts if the snapshot raced a write.
* Writers are unpaused in a ``finally`` block, whatever happens. A writer is
  listed as possibly paused BEFORE its pause is issued; every listed writer is
  tried, each with retries, a timed-out or failed ``docker unpause`` is
  followed by a check of the container's paused state, and an interrupt
  arriving meanwhile is held until every writer has been tried. Anything still
  paused is reported with the manual command. SIGTERM and SIGHUP (a dropped
  terminal) interrupt the run the same way Ctrl-C does, so the ``finally``
  block runs.
* Every artifact is read back from the destination and its SHA-256 compared
  before it is recorded; a failed command leaves no partial artifact.
* Source archives are fingerprinted before and after; a change aborts.
* Each logical dump is decoded by ``pg_restore --list``. Each physical backup is
  extracted and checked with ``pg_verifybackup``, including a corruption
  negative control -- unless the verification disk lacks the archive's
  rounded size plus a reserve, in which case that cluster is reported as
  ``not_run_insufficient_local_space``, never as passed.
* The ``CURRENT.json`` pointer is published only after everything above, and
  it states which clusters were physically verified and which were not.

Dry run by default: without ``--execute`` it prints the plan and runs nothing.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import traceback
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import FrameType
from typing import IO, Protocol
from uuid import uuid4

from scripts.staging.opslib import (
    CHUNK_BYTES,
    REPO_ROOT,
    CommandFailed,
    CommandRunner,
    JsonObject,
    OperationFailed,
    SubprocessRunner,
    UsageError,
    add_execute_flag,
    capture,
    emit,
    require_dict,
    require_list,
    run_tool,
    setting,
)

HMAC_ENV_PREFIX = "ELSPETH_JUDGE_METADATA_HMAC_KEY="
POSTGRES_DATA_DIR = "/var/lib/postgresql/data"
DEFAULT_CACHE_EXCLUDES = (
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node-compile-cache",
    "node_modules",
)
DEFAULT_HOST_CONFIG = ("nginx", "docker", "systemd/system", "cron.d", "crontab")
_KEY_SEGMENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

ACTIVITY_SQL = (
    "select json_build_object("
    "'composer',(select count(*) from composer_async_operations where status in ('queued','running')),"
    "'runs',(select count(*) from runs where status in ('pending','queued','running')),"
    "'states',(select count(*) from composition_states),"
    "'messages',(select count(*) from chat_messages),"
    "'blobs',(select count(*) from blobs))"
)

# Runs as root (sudo -n python3 -B -c ...). argv: <root> <json list of excluded names>.
SOURCE_FINGERPRINT = """\
import hashlib, json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
excluded = set(json.loads(sys.argv[2]))
records = []
def fail(error):
    raise error
for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
    relative = Path(directory).relative_to(root)
    dirs[:] = sorted(name for name in dirs if name not in excluded)
    for name in sorted(files + [name for name in dirs if (Path(directory) / name).is_symlink()]):
        if name in excluded:
            continue
        path = Path(directory) / name
        st = path.lstat()
        link = os.readlink(path) if path.is_symlink() else None
        records.append((str(relative / name), st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino, link))
print(hashlib.sha256(json.dumps(records, separators=(",", ":")).encode()).hexdigest())
"""

DISK_FREE = "import shutil, sys; print(shutil.disk_usage(sys.argv[1]).free)"
# argv: <target>; stdin: the gzipped physical archive.
EXTRACT = (
    "import sys, tarfile\nfrom pathlib import Path\np = Path(sys.argv[1])\np.mkdir(parents=True, exist_ok=False)\n"
    "with tarfile.open(fileobj=sys.stdin.buffer, mode='r|gz') as t:\n    t.extractall(p, filter='data')\n"
)
# argv: <target>; prints the original PG_VERSION bytes (hex), then writes a corrupt value.
CORRUPT_PG_VERSION = (
    "import sys\nfrom pathlib import Path\np = Path(sys.argv[1]) / 'PG_VERSION'\noriginal = p.read_bytes()\n"
    "assert original.rstrip(b'\\n').isdigit(), 'PG_VERSION is not a version number'\n"
    "p.write_bytes(b'XX\\n')\nprint(original.hex())\n"
)
# argv: <target> <original bytes hex>
RESTORE_PG_VERSION = "import sys\nfrom pathlib import Path\n(Path(sys.argv[1]) / 'PG_VERSION').write_bytes(bytes.fromhex(sys.argv[2]))\n"
REMOVE_TREE = "import shutil, sys; shutil.rmtree(sys.argv[1])"

RESTORE_TEXT = """\
Restore into approved storage with application writers stopped.
Load instance/images.tar.gz; reconstruct networks, volumes, environment, ports and restart
policies from instance/*.private.json. These files and the configuration archives contain secrets.
Each postgres/<container>/physical.tar.gz holds its whole cluster and recovery WAL; restore it with
the matching saved PostgreSQL image and the original volume ownership. Logical dumps and
globals.sql are an alternative path. manifest.json records, per cluster, whether pg_verifybackup
passed or was NOT RUN for lack of local space; an unverified physical archive must be verified
before it is relied on.
Restore volumes/*.tar.gz to their volumes, preserving ownership. They were captured with
application writers paused. Restore bind mounts and host configuration from their maps.
filesystems/*.tar.gz preserve each application container's writable layer; assess them together
with the saved image when reconstructing a container.
When present, extract source/working-directories.tar.gz and source/repository-git.tar.gz at the
project parent; run `git worktree repair` if the project path changes.
Verify authentication, readiness, secrets, file references and one controlled Composer turn before
reopening user access.
"""


# ── destination ────────────────────────────────────────────────────────────


class BackupStore(Protocol):
    """Where artifacts go. Every write is read back and its SHA-256 compared."""

    def write_stream(self, key: str, chunks: Iterable[bytes]) -> JsonObject: ...

    def open_read(self, key: str) -> AbstractContextManager[IO[bytes]]: ...

    def publish_pointer(self, key: str, data: bytes) -> None: ...


def check_key(key: str) -> str:
    """Refuse a key that is absolute, has an empty or dot segment, or unusual characters."""
    parts = key.split("/")
    for part in parts:
        if part in ("", ".", "..") or not set(part) <= _KEY_SEGMENT_CHARS:
            raise OperationFailed(f"artifact key {key!r} is not a safe relative path")
    return key


class LocalDirectoryStore:
    """A destination directory: a mounted backup share or approved volume, outside the repository."""

    def __init__(self, root: Path, *, repo_root: Path = REPO_ROOT) -> None:
        resolved = root.expanduser().resolve()
        if resolved == repo_root or resolved.is_relative_to(repo_root):
            raise UsageError("--destination must be outside the repository")
        if not resolved.is_dir():
            raise UsageError("--destination must be an existing directory")
        self.root = resolved

    def _path(self, key: str) -> Path:
        return self.root / PurePosixPath(check_key(key))

    def write_stream(self, key: str, chunks: Iterable[bytes]) -> JsonObject:
        path = self._path(key)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists():
            raise OperationFailed(f"artifact {key} already exists")
        partial = path.with_name(path.name + ".partial")
        digest = hashlib.sha256()
        size = 0
        try:
            descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                for chunk in chunks:
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists():
                raise OperationFailed(f"artifact {key} already exists")
            partial.rename(path)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        readback = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK_BYTES):
                readback.update(chunk)
        if readback.hexdigest() != digest.hexdigest():
            raise OperationFailed(f"artifact {key} read back with a different SHA-256")
        return {"key": key, "bytes": size, "sha256": digest.hexdigest(), "full_remote_readback_verified": True}

    def open_read(self, key: str) -> AbstractContextManager[IO[bytes]]:
        return self._path(key).open("rb")

    def publish_pointer(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if path.read_bytes() != data:
            raise OperationFailed(f"pointer {key} read back different bytes")


def read_chunks(store: BackupStore, key: str) -> Iterator[bytes]:
    with store.open_read(key) as handle:
        while chunk := handle.read(CHUNK_BYTES):
            yield chunk


# ── capture ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BackupConfig:
    postgres: tuple[str, ...]
    writers: tuple[str, ...]
    verification_container: str
    prefix: str
    lock_file: Path
    sessions_database: str = "elspeth_sessions"
    verification_mount: str = "/app/state"
    verification_subdir: str = "data/operator_tasks/backup-verification"
    source_root: Path | None = None
    source_excludes: tuple[str, ...] = DEFAULT_CACHE_EXCLUDES
    host_config_root: Path = Path("/etc")
    host_config: tuple[str, ...] = DEFAULT_HOST_CONFIG
    sudo: bool = True
    drain_poll_seconds: float = 30.0
    drain_timeout_seconds: float | None = None
    space_reserve_bytes: int = 128 * 1024 * 1024
    unpause_attempts: int = 3

    def __post_init__(self) -> None:
        if not self.postgres:
            raise UsageError("at least one --postgres container is required")
        if not self.writers:
            raise UsageError("at least one --writer container is required")
        if set(self.postgres) & set(self.writers):
            raise UsageError("a container cannot be both --postgres and --writer")
        if self.verification_container not in self.writers:
            raise UsageError("--verification-container must be one of the --writer containers")
        check_key(self.prefix)


@dataclass
class RecoveryCapture:
    config: BackupConfig
    runner: CommandRunner
    store: BackupStore
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    label: str = field(default_factory=lambda: datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    report: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.report = {
            "status": "capturing",
            "prefix": self.base,
            "artifacts": {},
            "started_at": datetime.now(UTC).isoformat(),
        }

    @property
    def base(self) -> str:
        return f"{self.config.prefix}/{self.label}/"

    # -- commands --------------------------------------------------------

    def privileged(self, *argv: str) -> list[str]:
        return [*(["sudo", "-n"] if self.config.sudo else []), *argv]

    def docker(self, *argv: str) -> list[str]:
        return self.privileged("docker", *argv)

    def pg_command(self, container: str, *argv: str, interactive: bool = False) -> list[str]:
        return self.docker(
            "exec",
            *(["-i"] if interactive else []),
            container,
            "sh",
            "-c",
            'PGUSER="$POSTGRES_USER" PGPASSWORD="$POSTGRES_PASSWORD" exec "$@"',
            "backup",
            *argv,
        )

    def python(self, code: str, *argv: str) -> list[str]:
        return self.privileged("python3", "-B", "-c", code, *argv)

    # -- artifacts -------------------------------------------------------

    def small(self, name: str, value: object) -> JsonObject:
        data = value if isinstance(value, bytes) else json.dumps(value, indent=2, default=str, sort_keys=True).encode()
        record = self.store.write_stream(self.base + name, [data])
        self.report["artifacts"][name] = record
        return record

    def checkpoint(self) -> None:
        self.store.write_stream(self.base + "progress/" + uuid4().hex + ".json", [json.dumps(self.report, default=str).encode()])

    def stream_artifact(self, name: str, argv: Sequence[str], *, gzip: bool = False) -> JsonObject:
        try:
            record = self.store.write_stream(self.base + name, self.runner.stream(argv, gzip=gzip))
        except CommandFailed as exc:
            self.small(
                "failed-command-" + uuid4().hex + ".json",
                {"command": list(argv), "exit_code": exc.returncode, "stderr": exc.stderr.decode(errors="replace")},
            )
            raise OperationFailed(f"archive command for {name} failed; details retained in the destination") from None
        self.report["artifacts"][name] = record
        self.checkpoint()
        emit("artifact_verified", artifact=name, bytes=record["bytes"])
        return record

    # -- inventory and activity -----------------------------------------

    def inspect_running(self) -> list[JsonObject]:
        ids = capture(self.runner, self.docker("ps", "-q", "--no-trunc")).decode().split()
        if not ids:
            raise OperationFailed("no running containers")
        infos = require_list(json.loads(capture(self.runner, self.docker("inspect", *ids))), "docker inspect")
        return [require_dict(item, "container") for item in infos]

    def activity(self, container: str) -> JsonObject:
        output = capture(
            self.runner,
            self.pg_command(container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", self.config.sessions_database, "-Atc", ACTIVITY_SQL),
        )
        return require_dict(json.loads(output), f"activity in {container}")

    def all_activity(self) -> dict[str, JsonObject]:
        return {name: self.activity(name) for name in self.config.postgres}

    def wait_for_drain(self) -> dict[str, JsonObject]:
        """Block until no Composer operation and no run is active on any cluster."""
        started = self.monotonic()
        while True:
            current = self.all_activity()
            if all(value["composer"] == 0 and value["runs"] == 0 for value in current.values()):
                return current
            timeout = self.config.drain_timeout_seconds
            if timeout is not None and self.monotonic() - started >= timeout:
                raise OperationFailed("active Composer or run work did not drain before --drain-timeout-seconds")
            emit("waiting_for_active_work", activity=current)
            self.sleep(self.config.drain_poll_seconds)

    # -- sources ---------------------------------------------------------

    def archive_source(self, name: str, root: Path, *, git_root: bool = False) -> JsonObject:
        excluded = set(self.config.source_excludes)
        if not git_root:
            excluded.add(".git")
        fingerprint = self.python(SOURCE_FINGERPRINT, str(root), json.dumps(sorted(excluded)))
        before = capture(self.runner, fingerprint).decode().strip()
        record = self.stream_artifact(
            name,
            self.privileged("tar", *["--exclude=" + item for item in sorted(excluded)], "-czf", "-", "-C", str(root.parent), root.name),
        )
        if capture(self.runner, fingerprint).decode().strip() != before:
            raise OperationFailed(f"source {root} changed during its archive")
        record.update(source=str(root), source_fingerprint=before)
        return record

    # -- databases and volumes while writers are paused -------------------

    def capture_paused(
        self, before: dict[str, JsonObject], by_name: dict[str, JsonObject], volumes: dict[str, JsonObject]
    ) -> dict[str, list[str]]:
        pg_volumes = {
            mount["Name"] for name in self.config.postgres for mount in by_name[name]["Mounts"] if mount["Destination"] == POSTGRES_DATA_DIR
        }
        databases: dict[str, list[str]] = {}
        paused: list[str] = []
        try:
            for name in self.config.writers:
                # Listed first: an interrupt between issuing the pause and its
                # return must still lead to an unpause attempt.
                paused.append(name)
                capture(self.runner, self.docker("pause", name))
            emit("application_writers_paused", containers=paused)
            if self.all_activity() != before:
                raise OperationFailed("activity changed before writers were paused; retry when idle")
            for name in self.config.postgres:
                listing = capture(
                    self.runner,
                    self.pg_command(
                        name,
                        "psql",
                        "-X",
                        "-d",
                        "postgres",
                        "-Atc",
                        "select datname from pg_database where not datistemplate order by datname",
                    ),
                )
                databases[name] = listing.decode().splitlines()
                for database in databases[name]:
                    self.stream_artifact(f"postgres/{name}/{database}.dump", self.pg_command(name, "pg_dump", "--format=custom", database))
                self.stream_artifact(f"postgres/{name}/globals.sql", self.pg_command(name, "pg_dumpall", "--globals-only"))
                self.stream_artifact(
                    f"postgres/{name}/physical.tar.gz",
                    self.pg_command(
                        name,
                        "pg_basebackup",
                        "-h",
                        "127.0.0.1",
                        "--format=tar",
                        "--wal-method=fetch",
                        "--pgdata=-",
                        "--checkpoint=fast",
                        "--manifest-checksums=SHA256",
                        "--no-password",
                    ),
                    gzip=True,
                )
            for volume_name, volume in volumes.items():
                if volume_name not in pg_volumes:
                    self.stream_artifact(
                        f"volumes/{volume_name}.tar.gz", self.privileged("tar", "czf", "-", "-C", volume["Mountpoint"], ".")
                    )
            after = self.all_activity()
            self.report["activity_after"] = after
            if after != before:
                raise OperationFailed("activity changed during capture; review concurrent work before using this snapshot")
        finally:
            self.unpause(paused)
        return databases

    def is_paused(self, name: str) -> bool | None:
        """The container's paused state, or ``None`` when it cannot be read."""
        try:
            result = self.runner.run(self.docker("container", "inspect", "--format", "{{.State.Paused}}", name), timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            return None
        state = result.stdout.strip()
        if result.returncode != 0 or state not in (b"true", b"false"):
            return None
        return state == b"true"

    def unpause_one(self, name: str) -> bool:
        """Try to unpause one container; ``True`` once it is known not to be paused."""
        for attempt in range(self.config.unpause_attempts):
            try:
                if self.runner.run(self.docker("unpause", name), timeout=60).returncode == 0:
                    return True
            except (subprocess.TimeoutExpired, OSError) as exc:
                emit("unpause_attempt_failed", container=name, attempt=attempt + 1, error=type(exc).__name__)
            # A container whose pause never took effect refuses an unpause: it is not paused either.
            if self.is_paused(name) is False:
                return True
            self.sleep(1)
        return False

    def unpause(self, paused: Sequence[str]) -> None:
        """Unpause every listed writer; report loudly any that may still be paused."""
        failed: list[str] = []
        interrupted: KeyboardInterrupt | None = None
        for name in reversed(paused):
            try:
                resumed = self.unpause_one(name)
            except KeyboardInterrupt as exc:
                # A second interrupt must not leave the remaining writers paused.
                interrupted = exc
                resumed = False
            if not resumed:
                failed.append(name)
        if failed:
            manual = " ".join(self.docker("unpause", *failed))
            emit("writers_still_paused", containers=failed, manual_command=manual)
            print(f"\nWARNING: application containers may still be paused: {', '.join(failed)}\nRun: {manual}\n", file=sys.stderr)
        if interrupted is not None:
            raise interrupted
        if failed:
            raise OperationFailed(f"containers still paused and need a manual unpause: {failed}")
        if paused:
            emit("application_writers_resumed", containers=list(paused))

    # -- verification ----------------------------------------------------

    def verify_logical(self, container: str, key: str) -> None:
        try:
            self.runner.feed(self.pg_command(container, "pg_restore", "--list", interactive=True), read_chunks(self.store, key))
        except CommandFailed as exc:
            self.small(
                "verification-error-" + uuid4().hex + ".json", {"exit_code": exc.returncode, "stderr": exc.stderr.decode(errors="replace")}
            )
            raise OperationFailed(f"pg_restore could not decode {key}") from None

    def verify_physical(self, container: str, image: str, key: str, portal_volume: str, portal_root: str) -> JsonObject:
        """Check one physical archive natively, or report that the check was not run."""
        with self.store.open_read(key) as handle, tarfile.open(fileobj=handle, mode="r|gz") as members:
            required = sum(((member.size + 4095) // 4096) * 4096 for member in members if member.isfile())
        available = int(capture(self.runner, self.python(DISK_FREE, portal_root)).decode().strip())
        if required + self.config.space_reserve_bytes > available:
            emit("physical_verification_not_run", container=container, required_bytes=required, available_bytes=available)
            return {
                "pg_verifybackup": "not_run_insufficient_local_space",
                "archive_file_bytes_rounded": required,
                "available_bytes": available,
                "remote_sha256_readback_verified": self.report["artifacts"][self.archive_name(key)]["full_remote_readback_verified"],
            }
        relative = f"{self.config.verification_subdir}/{self.label}/{container}"
        target = str(Path(portal_root) / relative)
        verify = self.docker(
            *("run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--user", "0:0"),
            *("--mount", f"type=volume,source={portal_volume},target=/recovery,readonly"),
            *("--entrypoint", "pg_verifybackup", image, "/recovery/" + relative),
        )
        try:
            self.runner.feed(self.python(EXTRACT, target), read_chunks(self.store, key))
            capture(self.runner, verify)
            original = capture(self.runner, self.python(CORRUPT_PG_VERSION, target)).decode().strip()
            if self.runner.run(verify).returncode == 0:
                raise OperationFailed(f"pg_verifybackup accepted a corrupted copy of {container}; verification is not trustworthy")
            capture(self.runner, self.python(RESTORE_PG_VERSION, target, original))
            capture(self.runner, verify)
        finally:
            self.runner.run(self.python(REMOVE_TREE, target))
        return {"pg_verifybackup": "passed", "corruption_negative_control": "rejected", "restored_copy_reverified": True}

    def archive_name(self, key: str) -> str:
        return key.removeprefix(self.base)

    # -- the whole run ---------------------------------------------------

    def run(self) -> JsonObject:
        config = self.config
        infos = self.inspect_running()
        by_name = {item["Name"].lstrip("/"): item for item in infos}
        missing = sorted((set(config.postgres) | set(config.writers)) - set(by_name))
        if missing:
            raise OperationFailed(f"named containers are not running: {missing}")
        if any(value.startswith(HMAC_ENV_PREFIX) for item in infos for value in item["Config"]["Env"] or []):
            raise OperationFailed("a running container holds the judge-metadata HMAC key; refusing to copy it into a backup")
        self.report["activity_at_start"] = self.all_activity()
        self.report["containers"] = sorted(by_name)
        self.small("instance/containers.private.json", infos)
        images = sorted({item["Image"] for item in infos})
        self.small("instance/images.private.json", json.loads(capture(self.runner, self.docker("image", "inspect", *images))))
        networks = sorted(
            {network["NetworkID"] for item in infos for network in item["NetworkSettings"]["Networks"].values() if network["NetworkID"]}
        )
        if networks:
            self.small("instance/networks.private.json", json.loads(capture(self.runner, self.docker("network", "inspect", *networks))))
        volume_names = sorted({mount["Name"] for item in infos for mount in item["Mounts"] if mount["Type"] == "volume"})
        volumes: dict[str, JsonObject] = {}
        if volume_names:
            inspected = require_list(json.loads(capture(self.runner, self.docker("volume", "inspect", *volume_names))), "volumes")
            volumes = {item["Name"]: item for item in inspected}
        self.small("instance/volumes.private.json", volumes)
        self.checkpoint()
        emit("backup_started", prefix=self.base, running_containers=len(infos), postgres_clusters=len(config.postgres))
        self.stream_artifact("instance/images.tar.gz", self.docker("image", "save", *images), gzip=True)
        if config.source_root is not None:
            self.report["artifacts"]["source/repository-git.tar.gz"] = self.archive_source(
                "source/repository-git.tar.gz", config.source_root / ".git", git_root=True
            )
            self.report["artifacts"]["source/working-directories.tar.gz"] = self.archive_source(
                "source/working-directories.tar.gz", config.source_root
            )
            self.checkpoint()
        before = self.wait_for_drain()
        self.report["activity_before"] = before
        for name, item in by_name.items():
            if name in config.postgres or name in config.writers:
                current = require_list(json.loads(capture(self.runner, self.docker("inspect", name))), name)
                if current[0]["Id"] != item["Id"]:
                    raise OperationFailed(f"container {name} was replaced during the run")
        databases = self.capture_paused(before, by_name, volumes)
        self.report["databases"] = databases
        portal = next(
            (mount for mount in by_name[config.verification_container]["Mounts"] if mount["Destination"] == config.verification_mount),
            None,
        )
        if portal is None or portal["Type"] != "volume":
            raise OperationFailed(f"{config.verification_container} has no volume mounted at {config.verification_mount}")
        verification: dict[str, JsonObject] = {}
        for name in config.postgres:
            for database in databases[name]:
                self.verify_logical(name, self.base + f"postgres/{name}/{database}.dump")
            verification[name] = self.verify_physical(
                name, by_name[name]["Image"], self.base + f"postgres/{name}/physical.tar.gz", portal["Name"], portal["Source"]
            )
            emit("database_archives_checked", container=name, physical=verification[name]["pg_verifybackup"])
        self.report["physical_verification"] = verification
        for name in config.writers:
            self.stream_artifact(f"filesystems/{name}.tar.gz", self.docker("export", name), gzip=True)
        bindings: list[JsonObject] = []
        for item in infos:
            for mount in item["Mounts"]:
                if mount["Type"] == "bind":
                    source = Path(mount["Source"])
                    artifact = f"configuration/bind-{len(bindings)}.tar.gz"
                    self.stream_artifact(artifact, self.privileged("tar", "czf", "-", "-C", str(source.parent), source.name))
                    bindings.append({"container": item["Name"], "mount": mount, "artifact": artifact})
        self.small("configuration/binds.private.json", bindings)
        present = [path for path in config.host_config if (config.host_config_root / path).exists()]
        if present:
            self.stream_artifact(
                "configuration/host.tar.gz", self.privileged("tar", "czf", "-", "-C", str(config.host_config_root), *present)
            )
        if config.source_root is not None:
            self.small(
                "source/worktrees.txt", capture(self.runner, ["git", "-C", str(config.source_root), "worktree", "list", "--porcelain"])
            )
        self.small("RESTORE.txt", RESTORE_TEXT.encode())
        passed = sorted(name for name, value in verification.items() if value["pg_verifybackup"] == "passed")
        not_run = sorted(name for name, value in verification.items() if value["pg_verifybackup"] != "passed")
        self.report.update(
            status="complete",
            completed_at=datetime.now(UTC).isoformat(),
            all_remote_readbacks_verified=True,
            all_logical_archives_native_readable=True,
            physical_verification_passed=passed,
            physical_verification_not_run=not_run,
        )
        manifest = self.store.write_stream(self.base + "manifest.json", [json.dumps(self.report, indent=2, default=str).encode()])
        pointer: JsonObject = {
            "status": "complete",
            "prefix": self.base,
            "manifest": manifest,
            "physical_verification_passed": passed,
            "physical_verification_not_run": not_run,
            "completed_at": self.report["completed_at"],
        }
        self.store.publish_pointer(f"{config.prefix}/CURRENT.json", json.dumps(pointer, indent=2).encode())
        emit(
            "full_recovery_backup_complete",
            prefix=self.base,
            artifacts=len(self.report["artifacts"]),
            manifest_sha256=manifest["sha256"],
            physical_verification_not_run=not_run,
        )
        return pointer

    def record_failure(self, exc: BaseException) -> None:
        self.report.update(status="failed", exception=type(exc).__name__, traceback=traceback.format_exc())
        try:
            self.store.write_stream(self.base + "failure.json", [json.dumps(self.report, indent=2, default=str).encode()])
        finally:
            emit("backup_failed", exception=type(exc).__name__, prefix=self.base)


# ── command line ───────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.staging.backup_vm_recovery",
        description=(
            "Capture a full recovery set of a single-host Docker deployment. Waits for Composer and run work to "
            "drain, pauses the --writer containers while databases and volumes are captured, unpauses them, and "
            "verifies every artifact. Dry run unless --execute."
        ),
    )
    parser.add_argument(
        "--postgres",
        action="append",
        default=[],
        metavar="CONTAINER",
        help="PostgreSQL container (repeat; env ELSPETH_OPS_POSTGRES_CONTAINERS, comma-separated)",
    )
    parser.add_argument(
        "--writer",
        action="append",
        default=[],
        metavar="CONTAINER",
        help="application container to pause and export: web and gateway (repeat; env ELSPETH_OPS_WRITER_CONTAINERS)",
    )
    parser.add_argument(
        "--verification-container",
        help="writer whose state volume receives the temporary physical-verification copy (env ELSPETH_OPS_VERIFICATION_CONTAINER)",
    )
    parser.add_argument(
        "--verification-mount", default="/app/state", help="mount point of that volume inside the container (default /app/state)"
    )
    parser.add_argument(
        "--sessions-database", default="elspeth_sessions", help="database holding the session tables (default elspeth_sessions)"
    )
    parser.add_argument("--destination", help="existing backup directory outside the repository (env ELSPETH_OPS_BACKUP_DESTINATION)")
    parser.add_argument(
        "--prefix", help="artifact prefix below the destination, e.g. <deployment>/recovery (env ELSPETH_OPS_BACKUP_PREFIX)"
    )
    parser.add_argument("--lock-file", help="existing lock file shared with the scheduled backup (env ELSPETH_OPS_BACKUP_LOCK)")
    parser.add_argument("--source-root", help="also archive this checkout and its .git directory (optional)")
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="NAME", help="extra directory or file name to leave out of source archives"
    )
    parser.add_argument(
        "--host-config",
        action="append",
        default=[],
        metavar="PATH",
        help=f"path under /etc to archive (repeat; default {', '.join(DEFAULT_HOST_CONFIG)})",
    )
    parser.add_argument("--no-sudo", action="store_true", help="run docker, tar and python3 without sudo -n")
    parser.add_argument("--drain-poll-seconds", type=float, default=30.0)
    parser.add_argument(
        "--drain-timeout-seconds",
        type=float,
        default=None,
        help="fail instead of waiting longer than this for work to drain (default: wait)",
    )
    add_execute_flag(parser)
    return parser


def _names(values: list[str], env: str) -> tuple[str, ...]:
    if values:
        return tuple(values)
    raw = os.environ.get(env, "")
    return tuple(name for name in (part.strip() for part in raw.split(",")) if name)


def config_from(args: argparse.Namespace) -> BackupConfig:
    source_root = Path(args.source_root).resolve() if args.source_root else None
    return BackupConfig(
        postgres=_names(args.postgres, "ELSPETH_OPS_POSTGRES_CONTAINERS"),
        writers=_names(args.writer, "ELSPETH_OPS_WRITER_CONTAINERS"),
        verification_container=setting(
            args.verification_container, flag="--verification-container", env="ELSPETH_OPS_VERIFICATION_CONTAINER"
        ),
        prefix=setting(args.prefix, flag="--prefix", env="ELSPETH_OPS_BACKUP_PREFIX").strip("/"),
        lock_file=Path(setting(args.lock_file, flag="--lock-file", env="ELSPETH_OPS_BACKUP_LOCK")),
        sessions_database=args.sessions_database,
        verification_mount=args.verification_mount,
        source_root=source_root,
        source_excludes=tuple(sorted(set(DEFAULT_CACHE_EXCLUDES) | set(args.exclude))),
        host_config=tuple(args.host_config) or DEFAULT_HOST_CONFIG,
        sudo=not args.no_sudo,
        drain_poll_seconds=args.drain_poll_seconds,
        drain_timeout_seconds=args.drain_timeout_seconds,
    )


def plan(config: BackupConfig, destination: str) -> JsonObject:
    return {
        "destination": destination,
        "prefix": config.prefix,
        "lock_file": str(config.lock_file),
        "wait_for_drain_on": list(config.postgres),
        "pause_then_unpause": list(config.writers),
        "postgres_dumps_and_physical_backups": list(config.postgres),
        "physical_verification_volume_of": f"{config.verification_container}:{config.verification_mount}",
        "source_root": str(config.source_root) if config.source_root is not None else None,
        "host_config": list(config.host_config),
        "sudo": config.sudo,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
    store_factory: Callable[[Path], BackupStore] = LocalDirectoryStore,
) -> int:
    args = build_parser().parse_args(argv)
    config = config_from(args)
    destination = setting(args.destination, flag="--destination", env="ELSPETH_OPS_BACKUP_DESTINATION")
    if not args.execute:
        emit("dry_run", plan=plan(config, destination), note="nothing was run; pass --execute to capture")
        return 0
    store = store_factory(Path(destination))
    capture_run = RecoveryCapture(config=config, runner=runner if runner is not None else SubprocessRunner(), store=store)
    try:
        lock = config.lock_file.open("r+b")
    except FileNotFoundError:
        raise UsageError(f"--lock-file {config.lock_file} does not exist") from None
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OperationFailed("another backup holds the lock") from None
        try:
            capture_run.run()
        except BaseException as exc:
            capture_run.record_failure(exc)
            raise
    return 0


STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def _stop_requested(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt(f"recovery capture interrupted by signal {signum}")


def install_stop_handlers() -> None:
    """Turn SIGTERM and SIGHUP into the same interrupt as Ctrl-C, so the unpause ``finally`` runs."""
    for number in STOP_SIGNALS:
        signal.signal(number, _stop_requested)


if __name__ == "__main__":
    install_stop_handlers()
    raise SystemExit(run_tool(main))


__all__ = ["BackupConfig", "BackupStore", "LocalDirectoryStore", "RecoveryCapture", "main"]
