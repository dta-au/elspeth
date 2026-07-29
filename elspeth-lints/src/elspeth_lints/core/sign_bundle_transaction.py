"""Durable scratch transactions for coherent ``sign-bundle`` publication.

Judge calls may take hours and can fail or be interrupted after earlier calls
have produced authoritative signatures.  Work therefore happens in a private,
same-filesystem copy of the configured allowlist.  The active directory is
changed only once, with Linux ``renameat2(RENAME_EXCHANGE)``, after every action
has succeeded and the candidate has been re-verified.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from elspeth_lints.core.atomic_io import allowlist_mutation_lock, atomic_update_text
from elspeth_lints.core.strict_json import StrictJSONError, strict_json_loads

SCHEMA_VERSION = 1
TRANSACTION_DIRNAME = ".sign-bundle-transactions"
MANIFEST_NAME = "transaction.json"
_MANIFEST_HMAC_FIELD = "manifest_hmac"
_MANIFEST_HMAC_LABEL = b"elspeth-sign-bundle-transaction-manifest-v1"
_SOURCE_VALIDATION_NOT_STARTED = "not_started"
_SOURCE_VALIDATION_PENDING = "pending"
_SOURCE_VALIDATION_VALIDATED = "validated"
_SOURCE_VALIDATION_STATES = frozenset(
    {
        _SOURCE_VALIDATION_NOT_STARTED,
        _SOURCE_VALIDATION_PENDING,
        _SOURCE_VALIDATION_VALIDATED,
    }
)
_RENAME_EXCHANGE = 2
_AT_FDCWD = -100
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)


class SignBundleTransactionError(RuntimeError):
    """The durable sign-bundle transaction is invalid or cannot be published."""


@dataclass(frozen=True, slots=True)
class SignBundleRunResult:
    exit_code: int
    completed_count: int
    failed_index: int | None = None
    failed_kind: str | None = None
    failed_key: str | None = None
    recovered_publish: bool = False


def tree_snapshot(root: Path) -> dict[str, str]:
    """Return a deterministic byte snapshot, rejecting symlink escape surfaces."""
    if not root.is_dir():
        raise SignBundleTransactionError(f"allowlist directory is missing: {root}")
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise SignBundleTransactionError(f"allowlist transaction refuses symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise SignBundleTransactionError(f"allowlist transaction refuses non-regular file: {path}")
        snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def directory_identity(path: Path) -> dict[str, int]:
    """Return the stable directory identity used to prove RENAME_EXCHANGE."""
    try:
        details = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SignBundleTransactionError(f"cannot stat allowlist transaction directory {path}: {exc}") from exc
    if not stat.S_ISDIR(details.st_mode):
        raise SignBundleTransactionError(f"allowlist transaction path is not a directory: {path}")
    return {"st_dev": details.st_dev, "st_ino": details.st_ino}


def transaction_root(allowlist_dir: Path) -> Path:
    return allowlist_dir.resolve().parent / TRANSACTION_DIRNAME


@contextmanager
def transaction_lock(allowlist_dir: Path, *, create: bool) -> Iterator[None]:
    """Serialize transaction creation/resume/publication for one allowlist parent."""
    root = transaction_root(allowlist_dir)
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise SignBundleTransactionError(f"sign-bundle transaction root is missing: {root}")
    lock_path = root / ".lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def create_transaction(
    *,
    bundle_path: Path,
    verified_bundle_sha256: str,
    bundle_id: str,
    root: Path,
    allowlist_dir: Path,
    rotation_log: Path,
    signing_policy: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Copy the active allowlist and initialize a secret-free durable journal."""
    active = allowlist_dir.resolve()
    source_root = root.resolve()
    bundle_resolved = bundle_path.resolve()
    rotation_resolved = rotation_log.resolve()
    if file_sha256(bundle_resolved) != verified_bundle_sha256:
        raise SignBundleTransactionError("bundle bytes changed after verification; refusing transaction creation")

    tx_root = transaction_root(active)
    tx_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _fsync_directory(active.parent)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", bundle_id).strip(".-") or "bundle"
    tx_path = Path(tempfile.mkdtemp(prefix=f"{safe_id}-", dir=tx_root))
    _fsync_directory(tx_root)
    _probe_exchange_support(tx_path)
    candidate_parent = tx_path / "candidate"
    candidate_parent.mkdir()
    candidate = candidate_parent / active.name
    with allowlist_mutation_lock(active):
        base_directory_identity = directory_identity(active)
        base_snapshot = tree_snapshot(active)
        shutil.copytree(active, candidate)
        candidate_directory_identity = directory_identity(candidate)
        if directory_identity(active) != base_directory_identity or tree_snapshot(candidate) != base_snapshot:
            raise SignBundleTransactionError("active allowlist changed while creating its transaction candidate")

    rotation_base = tx_path / "rotation-base.bin"
    rotation_staged = tx_path / "rotation-staged.log"
    base_rotation_bytes = rotation_resolved.read_bytes() if rotation_resolved.is_file() else b""
    rotation_base.write_bytes(base_rotation_bytes)
    rotation_staged.write_bytes(base_rotation_bytes)

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "bundle_path": str(bundle_resolved),
        "bundle_sha256": verified_bundle_sha256,
        "root": str(source_root),
        "allowlist_dir": str(active),
        "candidate_dir": str(candidate),
        "rotation_log": str(rotation_resolved),
        "signing_policy": signing_policy,
        "base_snapshot": base_snapshot,
        "candidate_snapshot": tree_snapshot(candidate),
        "base_directory_identity": base_directory_identity,
        "candidate_directory_identity": candidate_directory_identity,
        "rotation_base_sha256": file_sha256(rotation_base),
        "rotation_staged_sha256": file_sha256(rotation_staged),
        "checkpoint_snapshot": None,
        "publish_started_at": None,
        "source_validation_state": _SOURCE_VALIDATION_NOT_STARTED,
        "completed_actions": [],
        "running_action": None,
    }
    _fsync_tree(candidate)
    _fsync_directory(candidate_parent)
    _fsync_file(rotation_base)
    _fsync_file(rotation_staged)
    save_manifest(tx_path, manifest)
    return tx_path, manifest


def _transaction_manifest_key() -> bytes:
    """Derive a purpose-separated journal key without persisting key material."""
    from elspeth_lints.core.allowlist import _judge_metadata_hmac_key

    operator_key = _judge_metadata_hmac_key()
    return hmac.digest(operator_key, _MANIFEST_HMAC_LABEL, "sha256")


def _manifest_hmac(manifest: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in manifest.items() if key != _MANIFEST_HMAC_FIELD}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(_transaction_manifest_key(), canonical, hashlib.sha256).hexdigest()


def _source_validation_state(manifest: dict[str, Any]) -> str:
    """Return the authenticated publication-validation state, including legacy journals."""
    if "source_validation_state" in manifest:
        value = manifest["source_validation_state"]
    else:
        # A finalized rotation-log delta is a durable post-validation witness:
        # finalize_rotation_log runs only after source validation succeeds.
        # Without that witness, a legacy publish timestamp remains pending.
        value = (
            _SOURCE_VALIDATION_VALIDATED
            if manifest.get("publish_started_at") is not None and _legacy_rotation_log_finalized(manifest)
            else (_SOURCE_VALIDATION_PENDING if manifest.get("publish_started_at") is not None else _SOURCE_VALIDATION_NOT_STARTED)
        )
    if not isinstance(value, str) or value not in _SOURCE_VALIDATION_STATES:
        raise SignBundleTransactionError("transaction manifest source_validation_state is invalid")
    return value


def _legacy_rotation_log_finalized(manifest: dict[str, Any]) -> bool:
    """Whether a field-less legacy journal has a committed rotation witness."""
    try:
        candidate = Path(manifest["candidate_dir"])
        tx_path = candidate.parent.parent
        base = (tx_path / "rotation-base.bin").read_bytes()
        staged = (tx_path / "rotation-staged.log").read_bytes()
        if not staged.startswith(base):
            return False
        delta = _canonical_rotation_delta(
            staged[len(base) :],
            allowlist_dir=Path(manifest["allowlist_dir"]),
            publish_started_at=cast("str", manifest["publish_started_at"]),
        )
        if not delta:
            return False
        current = Path(manifest["rotation_log"]).read_bytes()
        if not current.startswith(base):
            return False
        suffix_lines = current[len(base) :].splitlines(keepends=True)
        delta_lines = delta.splitlines(keepends=True)
        return any(
            suffix_lines[index : index + len(delta_lines)] == delta_lines for index in range(len(suffix_lines) - len(delta_lines) + 1)
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


def source_validation_pending(manifest: dict[str, Any]) -> bool:
    """Whether an exchanged candidate still needs source-binding finalization."""
    return _source_validation_state(manifest) == _SOURCE_VALIDATION_PENDING


def load_manifest(tx_path: Path) -> dict[str, Any]:
    resolved = tx_path.resolve()
    manifest_path = resolved / MANIFEST_NAME
    try:
        raw = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, StrictJSONError) as exc:
        raise SignBundleTransactionError(f"cannot read transaction manifest {manifest_path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise SignBundleTransactionError(f"unsupported sign-bundle transaction manifest: {manifest_path}")
    recorded_hmac = raw.get(_MANIFEST_HMAC_FIELD)
    if not isinstance(recorded_hmac, str) or not hmac.compare_digest(recorded_hmac, _manifest_hmac(raw)):
        raise SignBundleTransactionError("transaction manifest authentication failed; refusing untrusted recovery state")
    required_strings = ("bundle_path", "bundle_sha256", "root", "allowlist_dir", "candidate_dir", "rotation_log")
    for key in required_strings:
        if not isinstance(raw.get(key), str):
            raise SignBundleTransactionError(f"transaction manifest field {key!r} must be a string")
    if not isinstance(raw.get("base_snapshot"), dict) or not isinstance(raw.get("candidate_snapshot"), dict):
        raise SignBundleTransactionError("transaction manifest snapshots must be mappings")
    for key in ("base_directory_identity", "candidate_directory_identity"):
        identity = raw.get(key)
        if (
            not isinstance(identity, dict)
            or set(identity) != {"st_dev", "st_ino"}
            or any(type(identity[field]) is not int or identity[field] < 0 for field in ("st_dev", "st_ino"))
        ):
            raise SignBundleTransactionError(f"transaction manifest field {key!r} must be a strict directory identity")
    if not isinstance(raw.get("signing_policy"), dict):
        raise SignBundleTransactionError("transaction manifest signing_policy must be a mapping")
    if not isinstance(raw.get("completed_actions"), list):
        raise SignBundleTransactionError("transaction manifest completed_actions must be a list")
    if not isinstance(raw.get("rotation_base_sha256"), str) or not isinstance(raw.get("rotation_staged_sha256"), str):
        raise SignBundleTransactionError("transaction manifest rotation hashes must be strings")
    if raw.get("checkpoint_snapshot") is not None and not isinstance(raw.get("checkpoint_snapshot"), dict):
        raise SignBundleTransactionError("transaction manifest checkpoint_snapshot must be a mapping or null")
    if raw.get("publish_started_at") is not None and not _is_timezone_aware_iso8601(raw.get("publish_started_at")):
        raise SignBundleTransactionError("transaction manifest publish_started_at must be a timezone-aware timestamp or null")
    _source_validation_state(raw)
    candidate = Path(raw["candidate_dir"]).resolve()
    if candidate.parent.parent != resolved:
        raise SignBundleTransactionError("transaction candidate path escapes its transaction directory")
    rotation_base = resolved / "rotation-base.bin"
    if not rotation_base.is_file() or file_sha256(rotation_base) != raw["rotation_base_sha256"]:
        raise SignBundleTransactionError("transaction rotation-base authentication failed")
    checkpoint_snapshot = raw.get("checkpoint_snapshot")
    checkpoint = resolved / "checkpoint"
    if checkpoint_snapshot is not None and (not checkpoint.is_dir() or tree_snapshot(checkpoint) != checkpoint_snapshot):
        raise SignBundleTransactionError("transaction checkpoint authentication failed")
    # An orphan checkpoint is the safe side of the two-phase protocol: the
    # authenticated manifest says no action depends on it, so a crash before
    # its best-effort deletion cannot strand recovery. The next action replaces
    # it before journalling a new running_action.
    return raw


def save_manifest(tx_path: Path, manifest: dict[str, Any]) -> None:
    manifest[_MANIFEST_HMAC_FIELD] = _manifest_hmac(manifest)
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    atomic_update_text(tx_path / MANIFEST_NAME, lambda _current: rendered, encoding="utf-8", create_parent=False)


def assert_resume_identity(
    manifest: dict[str, Any],
    *,
    bundle_path: Path,
    root: Path,
    allowlist_dir: Path,
    rotation_log: Path,
    signing_policy: dict[str, Any],
) -> None:
    expected = {
        "bundle_path": str(bundle_path.resolve()),
        "root": str(root.resolve()),
        "allowlist_dir": str(allowlist_dir.resolve()),
        "rotation_log": str(rotation_log.resolve()),
    }
    for field, value in expected.items():
        if manifest[field] != value:
            raise SignBundleTransactionError(f"resume {field} mismatch: transaction has {manifest[field]!r}, command selected {value!r}")
    if manifest["bundle_sha256"] != file_sha256(bundle_path.resolve()):
        raise SignBundleTransactionError("resume bundle bytes changed since the transaction was created")
    if manifest.get("signing_policy") != signing_policy:
        raise SignBundleTransactionError(
            "resume signing policy differs from the transaction (owner/override/transport/tools/repo-root/max-tokens/env-file/format)"
        )


def assert_active_unchanged(manifest: dict[str, Any]) -> None:
    active = Path(manifest["allowlist_dir"])
    if tree_snapshot(active) != manifest["base_snapshot"]:
        raise SignBundleTransactionError("active allowlist bytes changed since the transaction was created")


def publication_disposition(manifest: dict[str, Any]) -> str:
    """Classify the atomic swap, including a kill between exchange and journalling."""
    active = Path(manifest["allowlist_dir"])
    candidate = Path(manifest["candidate_dir"])
    active_snapshot = tree_snapshot(active)
    candidate_snapshot = tree_snapshot(candidate)
    active_identity = directory_identity(active)
    candidate_identity = directory_identity(candidate)
    original_orientation = (
        active_identity == manifest["base_directory_identity"] and candidate_identity == manifest["candidate_directory_identity"]
    )
    swapped_orientation = (
        active_identity == manifest["candidate_directory_identity"] and candidate_identity == manifest["base_directory_identity"]
    )
    if (
        original_orientation
        and active_snapshot == manifest["base_snapshot"]
        and (candidate_snapshot == manifest["candidate_snapshot"] or manifest.get("running_action") is not None)
    ):
        return "not_published"
    if swapped_orientation and active_snapshot == manifest["candidate_snapshot"] and candidate_snapshot == manifest["base_snapshot"]:
        return "published"
    if (
        swapped_orientation
        and manifest["candidate_snapshot"] != manifest["base_snapshot"]
        and active_snapshot != manifest["base_snapshot"]
        and candidate_snapshot == manifest["base_snapshot"]
        and manifest.get("publish_started_at") is not None
    ):
        # The authenticated candidate path can contain the base snapshot only
        # after RENAME_EXCHANGE. A coordinated writer may have changed the new
        # active tree after a post-exchange process death; preserve that later
        # mutation while still finalizing the already-committed rotation audit.
        return "published_active_drift"
    raise SignBundleTransactionError(
        "cannot reconcile transaction publication: active/candidate bytes or signatures match neither recorded atomic state"
    )


def assert_candidate_unchanged(manifest: dict[str, Any]) -> None:
    candidate = Path(manifest["candidate_dir"])
    if tree_snapshot(candidate) != manifest["candidate_snapshot"]:
        raise SignBundleTransactionError("transaction candidate bytes/signatures changed outside the recorded action journal")


def checkpoint_action_file(tx_path: Path, manifest: dict[str, Any], relative_path: str | None) -> None:
    """Save the one YAML a non-atomic action may have removed before interruption."""
    checkpoint = tx_path / "checkpoint"
    if checkpoint.exists():
        shutil.rmtree(checkpoint)
    checkpoint.mkdir()
    metadata: dict[str, Any] = {"relative_path": relative_path, "existed": False}
    if relative_path is not None:
        relative_path = _local_allowlist_filename(relative_path)
        metadata["relative_path"] = relative_path
        source = Path(manifest["candidate_dir"]) / relative_path
        metadata["existed"] = source.is_file()
        if source.is_file():
            shutil.copy2(source, checkpoint / "allowlist-file")
    rotation_staged = tx_path / "rotation-staged.log"
    if rotation_staged.is_file():
        shutil.copy2(rotation_staged, checkpoint / "rotation-staged.log")
    judge_events = Path(manifest["candidate_dir"]) / ".judge-metrics" / "judge-decision-events.jsonl"
    metadata["judge_events_existed"] = judge_events.is_file()
    if judge_events.is_file():
        shutil.copy2(judge_events, checkpoint / "judge-decision-events.jsonl")
    (checkpoint / "metadata.json").write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    _fsync_tree(checkpoint)
    manifest["checkpoint_snapshot"] = tree_snapshot(checkpoint)


def restore_action_checkpoint(tx_path: Path, manifest: dict[str, Any]) -> None:
    checkpoint = tx_path / "checkpoint"
    metadata_path = checkpoint / "metadata.json"
    if not metadata_path.is_file():
        return
    raw = strict_json_loads(metadata_path.read_text(encoding="utf-8"))
    relative = raw.get("relative_path")
    if isinstance(relative, str):
        relative = _local_allowlist_filename(relative)
        target = Path(manifest["candidate_dir"]) / relative
        saved = checkpoint / "allowlist-file"
        if raw.get("existed") is True:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)
        elif target.exists():
            target.unlink()
    saved_rotation = checkpoint / "rotation-staged.log"
    if saved_rotation.is_file():
        shutil.copy2(saved_rotation, tx_path / "rotation-staged.log")


def clear_action_checkpoint(tx_path: Path) -> None:
    checkpoint = tx_path / "checkpoint"
    if checkpoint.exists():
        shutil.rmtree(checkpoint)


def mark_candidate_snapshot(tx_path: Path, manifest: dict[str, Any]) -> None:
    """Durably record candidate and staged-audit bytes before journalling them."""
    candidate = Path(manifest["candidate_dir"])
    rotation_staged = tx_path / "rotation-staged.log"
    _fsync_tree(candidate)
    _fsync_file(rotation_staged)
    manifest["candidate_snapshot"] = tree_snapshot(Path(manifest["candidate_dir"]))
    manifest["rotation_staged_sha256"] = file_sha256(rotation_staged)


def commit_action_state(tx_path: Path, manifest: dict[str, Any]) -> None:
    """Authenticate durable action state before retiring its checkpoint."""
    manifest["checkpoint_snapshot"] = None
    mark_candidate_snapshot(tx_path, manifest)
    save_manifest(tx_path, manifest)
    clear_action_checkpoint(tx_path)
    _fsync_directory(tx_path)


def publish_candidate(tx_path: Path, manifest: dict[str, Any]) -> None:
    """Atomically exchange the coherent candidate with the active directory."""
    active = Path(manifest["allowlist_dir"])
    candidate = Path(manifest["candidate_dir"])
    candidate_snapshot = manifest["candidate_snapshot"]
    base_snapshot = manifest["base_snapshot"]

    with allowlist_mutation_lock(active):
        current_active = tree_snapshot(active)
        current_candidate = tree_snapshot(candidate)
        if current_active == candidate_snapshot and current_candidate == base_snapshot:
            return
        if current_active != base_snapshot or current_candidate != candidate_snapshot:
            raise SignBundleTransactionError("publish precondition failed: active or candidate bytes changed")

        _fsync_tree(candidate)
        _rename_exchange(active, candidate)
        for parent in {active.parent, candidate.parent}:
            parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)

        if tree_snapshot(active) != candidate_snapshot or tree_snapshot(candidate) != base_snapshot:
            raise SignBundleTransactionError("atomic exchange completed with unexpected directory content")


def rollback_pending_publish(tx_path: Path, manifest: dict[str, Any]) -> None:
    """Restore the authenticated base when an exchanged publish fails source validation."""
    if not source_validation_pending(manifest):
        raise SignBundleTransactionError("refusing source-validation rollback for a transaction that is not pending")
    active = Path(manifest["allowlist_dir"])
    candidate = Path(manifest["candidate_dir"])
    with allowlist_mutation_lock(active):
        disposition = publication_disposition(manifest)
        if disposition == "published":
            _rename_exchange(active, candidate)
            for parent in {active.parent, candidate.parent}:
                _fsync_directory(parent)
        elif disposition != "not_published":
            raise SignBundleTransactionError(
                "cannot roll back source-invalid publish after the active allowlist received later coordinated changes"
            )
        if (
            directory_identity(active) != manifest["base_directory_identity"]
            or directory_identity(candidate) != manifest["candidate_directory_identity"]
            or tree_snapshot(active) != manifest["base_snapshot"]
            or tree_snapshot(candidate) != manifest["candidate_snapshot"]
        ):
            raise SignBundleTransactionError("source-validation rollback did not restore the authenticated directory state")
        manifest["source_validation_state"] = _SOURCE_VALIDATION_NOT_STARTED
        manifest["publish_started_at"] = None
        save_manifest(tx_path, manifest)


def _validate_pending_source_publish(
    *,
    bundle: Any,
    args: argparse.Namespace,
    tx_path: Path,
    manifest: dict[str, Any],
) -> None:
    """Re-derive source bindings after exchange, rolling back before reporting drift."""
    state = _source_validation_state(manifest)
    if state == _SOURCE_VALIDATION_VALIDATED:
        return
    if state != _SOURCE_VALIDATION_PENDING:
        raise SignBundleTransactionError("published transaction has no pending source-validation record")
    from elspeth_lints.core.bundle_verify import verify_bundle_against_tree

    try:
        verification = verify_bundle_against_tree(
            bundle,
            root=args.root,
            # RENAME_EXCHANGE moved the exact pre-publish allowlist here. Re-run
            # the bundle claims against that base while the signed candidate is
            # active, so source drift cannot hide behind its own new entries.
            allowlist_dir=Path(manifest["candidate_dir"]),
        )
    except ValueError:
        rollback_pending_publish(tx_path, manifest)
        raise SignBundleTransactionError(
            "source tree or bundle bindings changed during coherent publish; rolled back active allowlist"
        ) from None
    if not verification.ok:
        rollback_pending_publish(tx_path, manifest)
        raise SignBundleTransactionError("source tree or bundle bindings changed during coherent publish; rolled back active allowlist")
    manifest["source_validation_state"] = _SOURCE_VALIDATION_VALIDATED
    save_manifest(tx_path, manifest)


def finalize_rotation_log(tx_path: Path, manifest: dict[str, Any]) -> None:
    """Append only transaction-produced rotation records after allowlist publish."""
    rotation_log = Path(manifest["rotation_log"])
    base = (tx_path / "rotation-base.bin").read_bytes()
    staged = (tx_path / "rotation-staged.log").read_bytes()
    if not staged.startswith(base):
        raise SignBundleTransactionError("transaction rotation log does not extend its recorded base")
    delta = _canonical_rotation_delta(
        staged[len(base) :],
        allowlist_dir=Path(manifest["allowlist_dir"]),
        publish_started_at=cast("str", manifest["publish_started_at"]),
    )
    if not delta:
        return
    delta_text = delta.decode("utf-8")

    def append_if_absent(existing: str | None) -> str:
        current = (existing or "").encode("utf-8")
        if not current.startswith(base):
            raise SignBundleTransactionError("rotation audit log no longer contains the transaction's recorded base")
        suffix_lines = current[len(base) :].splitlines(keepends=True)
        delta_lines = delta.splitlines(keepends=True)
        if any(suffix_lines[index : index + len(delta_lines)] == delta_lines for index in range(len(suffix_lines) - len(delta_lines) + 1)):
            return existing or ""
        return (existing or "") + delta_text

    # A separate append-only writer may add records after the allowlist commit.
    # Preserve that suffix and append ours under the log's own lock. Before the
    # commit, assert_rotation_log_unchanged still rejects any drift.
    atomic_update_text(
        rotation_log,
        append_if_absent,
        encoding="utf-8",
        create_parent=True,
    )


def assert_rotation_log_unchanged(tx_path: Path, manifest: dict[str, Any]) -> None:
    """Fail before active publish when the external append-only log drifted."""
    rotation_log = Path(manifest["rotation_log"])
    base = (tx_path / "rotation-base.bin").read_bytes()
    current = rotation_log.read_bytes() if rotation_log.is_file() else b""
    if current != base:
        raise SignBundleTransactionError("rotation audit log changed since the transaction was created")


_ACTION_PRIORITY = {
    "stale_delete": 0,
    "rotation": 1,
    "drift_repair": 2,
    "justify": 3,
}


def run_sign_bundle_transaction(
    *,
    bundle: Any,
    verification: Any,
    args: argparse.Namespace,
    tx_path: Path,
    manifest: dict[str, Any],
    disposition: str,
    specs_by_stale_key: dict[str, Any],
    execute_action: Callable[[Any, argparse.Namespace], int],
) -> SignBundleRunResult:
    """Reconcile/fire journaled actions and publish one verified candidate."""
    repair_keys_by_stale_key = {
        item.key: item.repair_key or item.key for item in verification.diagnosis.items if item.key in specs_by_stale_key
    }
    candidate_args = argparse.Namespace(**vars(args))
    candidate_args.allowlist_dir = args.allowlist_dir if disposition.startswith("published") else Path(manifest["candidate_dir"])
    candidate_args.rotation_log = tx_path / "rotation-staged.log"
    candidate_args._defer_override_rate_counter_snapshot = True
    completed = {int(index) for index in manifest["completed_actions"]}
    running = manifest.get("running_action")
    recorded_rotation_hash = cast("str", manifest["rotation_staged_sha256"])
    if running is None:
        if file_sha256(tx_path / "rotation-staged.log") != recorded_rotation_hash:
            raise SignBundleTransactionError("transaction staged rotation audit authentication failed")
    else:
        checkpoint_rotation = tx_path / "checkpoint" / "rotation-staged.log"
        if not checkpoint_rotation.is_file() or file_sha256(checkpoint_rotation) != recorded_rotation_hash:
            raise SignBundleTransactionError("transaction running-action rotation checkpoint authentication failed")

    if disposition.startswith("published"):
        if completed != set(range(len(bundle.actions))):
            raise SignBundleTransactionError("published transaction journal does not contain every bundle action")
        _validate_pending_source_publish(
            bundle=bundle,
            args=args,
            tx_path=tx_path,
            manifest=manifest,
        )
        if disposition == "published":
            _verify_completed_actions(
                bundle,
                completed=completed,
                verification=verification,
                repair_keys_by_stale_key=repair_keys_by_stale_key,
                args=candidate_args,
                tx_path=tx_path,
            )
        _assert_final_rotation_evidence(
            bundle,
            completed=completed,
            verification=verification,
            tx_path=tx_path,
        )
        with allowlist_mutation_lock(Path(manifest["allowlist_dir"])):
            finalize_rotation_log(tx_path, manifest)
        return SignBundleRunResult(0, len(completed), recovered_publish=True)

    if running is not None:
        running_index = int(running)
        if running_index < 0 or running_index >= len(bundle.actions):
            raise SignBundleTransactionError(f"transaction running_action index is out of range: {running_index}")
        running_action = bundle.actions[running_index]
        running_source_file = _action_source_file(
            running_action,
            verification=verification,
            specs_by_stale_key=specs_by_stale_key,
        )
        running_expected_key = _expected_action_key(
            running_action,
            verification=verification,
            repair_keys_by_stale_key=repair_keys_by_stale_key,
        )
        _assert_action_scoped_candidate_changes(
            running_action,
            tx_path=tx_path,
            manifest=manifest,
            source_file=running_source_file,
            expected_key=running_expected_key,
            verify_semantics=None,
        )
        running_complete = _action_is_complete(
            running_action,
            verification=verification,
            repair_keys_by_stale_key=repair_keys_by_stale_key,
            args=candidate_args,
            tx_path=tx_path,
            authoritative=True,
        )
        if running_complete:
            _prepare_completed_judge_evidence(
                running_action,
                tx_path=tx_path,
                candidate_dir=Path(manifest["candidate_dir"]),
                source_file=running_source_file,
                expected_key=running_expected_key,
            )
            _assert_action_scoped_candidate_changes(
                running_action,
                tx_path=tx_path,
                manifest=manifest,
                source_file=running_source_file,
                expected_key=running_expected_key,
                verify_semantics=True,
            )
            completed.add(running_index)
        else:
            _assert_judge_event_transition(
                running_action,
                tx_path=tx_path,
                candidate_dir=Path(manifest["candidate_dir"]),
                source_file=running_source_file,
                expected_key=running_expected_key,
                success=False,
            )
            _assert_rotation_staged_transition(
                running_action,
                tx_path=tx_path,
                source_file=running_source_file,
                expected_key=running_expected_key,
                success=False,
            )
            restore_action_checkpoint(tx_path, manifest)
        manifest["running_action"] = None
        manifest["completed_actions"] = sorted(completed)
        commit_action_state(tx_path, manifest)

    assert_candidate_unchanged(manifest)
    _verify_completed_actions(
        bundle,
        completed=completed,
        verification=verification,
        repair_keys_by_stale_key=repair_keys_by_stale_key,
        args=candidate_args,
        tx_path=tx_path,
    )
    _assert_final_rotation_evidence(
        bundle,
        completed=completed,
        verification=verification,
        tx_path=tx_path,
    )

    ordered_actions = sorted(
        enumerate(bundle.actions),
        key=lambda indexed: (_ACTION_PRIORITY[indexed[1].kind], indexed[0]),
    )
    for index, action in ordered_actions:
        if index in completed:
            continue
        source_file = _action_source_file(
            action,
            verification=verification,
            specs_by_stale_key=specs_by_stale_key,
        )
        checkpoint_action_file(
            tx_path,
            manifest,
            source_file,
        )
        manifest["running_action"] = index
        save_manifest(tx_path, manifest)
        try:
            code = execute_action(action, candidate_args)
        except BaseException:
            raise
        expected_key = _expected_action_key(
            action,
            verification=verification,
            repair_keys_by_stale_key=repair_keys_by_stale_key,
        )
        if code == 0:
            _prepare_completed_judge_evidence(
                action,
                tx_path=tx_path,
                candidate_dir=Path(manifest["candidate_dir"]),
                source_file=source_file,
                expected_key=expected_key,
            )
        _assert_action_scoped_candidate_changes(
            action,
            tx_path=tx_path,
            manifest=manifest,
            source_file=source_file,
            expected_key=expected_key,
            verify_semantics=code == 0,
        )
        if code != 0:
            manifest["running_action"] = None
            commit_action_state(tx_path, manifest)
            return SignBundleRunResult(
                code,
                len(completed),
                failed_index=index,
                failed_kind=action.kind,
                failed_key=action.key,
            )
        if not _action_is_complete(
            action,
            verification=verification,
            repair_keys_by_stale_key=repair_keys_by_stale_key,
            args=candidate_args,
            tx_path=tx_path,
            authoritative=False,
        ):
            raise SignBundleTransactionError(
                f"action reported success but its transaction result did not re-verify: {action.kind} {action.key}"
            )
        completed.add(index)
        manifest["completed_actions"] = sorted(completed)
        manifest["running_action"] = None
        commit_action_state(tx_path, manifest)

    _verify_completed_actions(
        bundle,
        completed=completed,
        verification=verification,
        repair_keys_by_stale_key=repair_keys_by_stale_key,
        args=candidate_args,
        tx_path=tx_path,
    )
    _assert_final_rotation_evidence(
        bundle,
        completed=completed,
        verification=verification,
        tx_path=tx_path,
    )
    from elspeth_lints.core.bundle_verify import verify_bundle_against_tree

    final_verification = verify_bundle_against_tree(
        bundle,
        root=args.root,
        allowlist_dir=args.allowlist_dir,
    )
    if not final_verification.ok:
        raise SignBundleTransactionError("source tree or bundle bindings changed during the transaction; refusing coherent publish")
    # The allowlist and append-only rotation log cannot share one rename domain.
    # The private transaction is the pending record: conflict-check the log
    # before the atomic allowlist commit, publish the coherent directory, then
    # append idempotently. A kill in that narrow post-commit window resumes from
    # disposition="published" and finalizes the exact staged delta.
    assert_rotation_log_unchanged(tx_path, manifest)
    if manifest["candidate_snapshot"] == manifest["base_snapshot"]:
        raise SignBundleTransactionError("refusing coherent publish without an authenticated candidate mutation")
    from datetime import UTC, datetime

    # Every fresh pre-exchange attempt gets a new authenticated start time.
    # A prior failed attempt or pre-exchange process death must not make the
    # eventual active mutation appear older than it is.
    manifest["publish_started_at"] = datetime.now(UTC).isoformat()
    manifest["source_validation_state"] = _SOURCE_VALIDATION_PENDING
    save_manifest(tx_path, manifest)
    with allowlist_mutation_lock(Path(manifest["allowlist_dir"])):
        publish_candidate(tx_path, manifest)
        _validate_pending_source_publish(
            bundle=bundle,
            args=args,
            tx_path=tx_path,
            manifest=manifest,
        )
        finalize_rotation_log(tx_path, manifest)
    return SignBundleRunResult(0, len(completed))


def _action_source_file(
    action: Any,
    *,
    verification: Any,
    specs_by_stale_key: dict[str, Any],
) -> str | None:
    if action.kind == "drift_repair":
        spec = specs_by_stale_key.get(action.key)
        source_file = None if spec is None else spec.stale_source_file
        return None if source_file is None else _local_allowlist_filename(source_file)
    if action.kind == "stale_delete":
        matching = [item for item in verification.diagnosis.items if item.key == action.key]
        if len(matching) != 1:
            raise SignBundleTransactionError(f"stale_delete {action.key!r} has {len(matching)} fresh diagnosis owners")
        return _local_allowlist_filename(cast("str", matching[0].source_file))
    if action.kind == "rotation":
        if verification.rotation_plan is None:
            raise SignBundleTransactionError("rotation action has no verified rotation plan")
        matching = [rotation for rotation in verification.rotation_plan.rotations if rotation.old_key == action.key]
        if len(matching) != 1:
            raise SignBundleTransactionError(f"rotation {action.key!r} has {len(matching)} verified owning files")
        return _local_allowlist_filename(cast("str", matching[0].entry_source_file))
    if action.kind == "justify":
        file_path = cast("str", action.file_path)
        if "/" in file_path:
            return _local_allowlist_filename(f"{file_path.split('/', 1)[0]}.yaml")
        stem = file_path.removesuffix(".py")
        return _local_allowlist_filename("cli.yaml" if stem.startswith("cli") else f"{stem}.yaml")
    return None


def _expected_action_key(
    action: Any,
    *,
    verification: Any,
    repair_keys_by_stale_key: dict[str, str],
) -> str:
    if action.kind == "drift_repair":
        return repair_keys_by_stale_key.get(cast("str", action.key), cast("str", action.key))
    if action.kind == "rotation":
        if verification.rotation_plan is None:
            raise SignBundleTransactionError("rotation action has no verified rotation plan")
        matching = [rotation for rotation in verification.rotation_plan.rotations if rotation.old_key == action.key]
        if len(matching) != 1:
            raise SignBundleTransactionError(f"rotation {action.key!r} has {len(matching)} verified target keys")
        return cast("str", matching[0].new_key)
    return cast("str", action.key)


def _assert_action_scoped_candidate_changes(
    action: Any,
    *,
    tx_path: Path,
    manifest: dict[str, Any],
    source_file: str | None,
    expected_key: str,
    verify_semantics: bool | None,
) -> None:
    """Reject mutations outside the exact verified action transition."""
    before = cast("dict[str, str]", manifest["candidate_snapshot"])
    after = tree_snapshot(Path(manifest["candidate_dir"]))
    changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    target = source_file
    allowed: set[str] = set()
    if target is not None:
        target = _local_allowlist_filename(target)
        allowed.update({target, f".{target}.lock"})
    if action.kind in {"justify", "drift_repair"}:
        allowed.update(
            {
                ".judge-metrics/judge-decision-events.jsonl",
                ".judge-metrics/.judge-decision-events.jsonl.lock",
            }
        )
    unexpected = sorted(changed - allowed)
    if unexpected:
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} changed unrelated candidate path(s): {', '.join(unexpected)}")
    _assert_judge_event_transition(
        action,
        tx_path=tx_path,
        candidate_dir=Path(manifest["candidate_dir"]),
        source_file=source_file,
        expected_key=expected_key,
        success=verify_semantics,
    )
    _assert_rotation_staged_transition(
        action,
        tx_path=tx_path,
        source_file=source_file,
        expected_key=expected_key,
        success=verify_semantics,
    )
    if target is not None and verify_semantics is not None:
        _assert_target_yaml_transition(
            action,
            tx_path=tx_path,
            candidate_dir=Path(manifest["candidate_dir"]),
            source_file=target,
            expected_key=expected_key,
            success=verify_semantics,
        )


def _assert_judge_event_transition(
    action: Any,
    *,
    tx_path: Path,
    candidate_dir: Path,
    source_file: str | None,
    expected_key: str,
    success: bool | None,
) -> None:
    checkpoint = tx_path / "checkpoint"
    metadata = strict_json_loads((checkpoint / "metadata.json").read_text(encoding="utf-8"))
    saved = checkpoint / "judge-decision-events.jsonl"
    if metadata.get("judge_events_existed") is True and not saved.is_file():
        raise SignBundleTransactionError("judge-event checkpoint is missing its before-image")
    before = saved.read_bytes() if metadata.get("judge_events_existed") is True else b""
    event_path = candidate_dir / ".judge-metrics" / "judge-decision-events.jsonl"
    after = event_path.read_bytes() if event_path.is_file() else b""
    if action.kind not in {"justify", "drift_repair"}:
        if after != before:
            raise SignBundleTransactionError(f"{action.kind} {action.key!r} changed the judge decision-event log")
        return
    if not after.startswith(before):
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} rewrote prior judge decision events")
    delta = after[len(before) :]
    if not delta:
        if success is True and candidate_dir.name.startswith("enforce_"):
            raise SignBundleTransactionError(f"{action.kind} {action.key!r} did not append its required decision event")
        return
    if before and not before.endswith(b"\n"):
        raise SignBundleTransactionError("judge decision-event before-image lacks a JSONL boundary")
    if not delta.endswith(b"\n"):
        raise SignBundleTransactionError("judge decision-event delta lacks a JSONL boundary")
    try:
        lines = delta.decode("utf-8").splitlines()
        records = [strict_json_loads(line) for line in lines]
    except (UnicodeDecodeError, StrictJSONError) as exc:
        raise SignBundleTransactionError("judge decision-event delta is not valid JSONL") from exc
    if len(records) != 1 or not isinstance(records[0], dict):
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} must append at most one decision event")
    record = cast("dict[str, Any]", records[0])
    required_fields = {
        "schema_version",
        "source_file",
        "entry_key",
        "rule_id",
        "effective_verdict",
        "model_verdict",
        "recorded_at",
        "write_disposition",
    }
    key_parts = expected_key.split(":")
    expected_dispositions = (
        {"written"} if success is True else {"blocked_without_override"} if success is False else {"written", "blocked_without_override"}
    )
    expected_verdict_pairs = (
        {
            ("ACCEPTED", "ACCEPTED"),
            ("OVERRIDDEN_BY_OPERATOR", "ACCEPTED"),
            ("OVERRIDDEN_BY_OPERATOR", "BLOCKED"),
        }
        if success is True
        else {("BLOCKED", "BLOCKED")}
        if success is False
        else {
            ("ACCEPTED", "ACCEPTED"),
            ("BLOCKED", "BLOCKED"),
            ("OVERRIDDEN_BY_OPERATOR", "ACCEPTED"),
            ("OVERRIDDEN_BY_OPERATOR", "BLOCKED"),
        }
    )
    verdict_pair = (record.get("effective_verdict"), record.get("model_verdict"))
    if (
        set(record) != required_fields
        or record.get("schema_version") != 1
        or record.get("entry_key") != expected_key
        or len(key_parts) < 2
        or record.get("source_file") != key_parts[0]
        or record.get("rule_id") != key_parts[1]
        or record.get("write_disposition") not in expected_dispositions
        or verdict_pair not in expected_verdict_pairs
        or not _is_timezone_aware_iso8601(record.get("recorded_at"))
    ):
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} appended an unrelated decision event")
    if success is True:
        expected_record = _signed_judge_event_payload(
            action,
            tx_path=tx_path,
            candidate_dir=candidate_dir,
            source_file=source_file,
            expected_key=expected_key,
        )
        if record != expected_record:
            raise SignBundleTransactionError(f"{action.kind} {action.key!r} decision event contradicts its authoritative signed entry")


def _assert_rotation_staged_transition(
    action: Any,
    *,
    tx_path: Path,
    source_file: str | None,
    expected_key: str,
    success: bool | None,
) -> None:
    checkpoint = tx_path / "checkpoint" / "rotation-staged.log"
    if not checkpoint.is_file():
        raise SignBundleTransactionError("rotation-staged checkpoint is missing")
    before = checkpoint.read_bytes()
    after = (tx_path / "rotation-staged.log").read_bytes()
    if action.kind != "rotation":
        if after != before:
            raise SignBundleTransactionError(f"{action.kind} {action.key!r} changed the staged rotation audit")
        return
    if success is False:
        if after != before:
            raise SignBundleTransactionError(f"failed rotation {action.key!r} changed the staged rotation audit")
        return
    if not after.startswith(before):
        raise SignBundleTransactionError(f"rotation {action.key!r} rewrote prior staged rotation audit records")
    if success is None:
        return
    delta = after[len(before) :]
    if before and not before.endswith(b"\n"):
        raise SignBundleTransactionError("rotation audit before-image lacks a JSONL boundary")
    if not delta.endswith(b"\n"):
        raise SignBundleTransactionError("staged rotation audit delta lacks a JSONL boundary")
    try:
        lines = delta.decode("utf-8").splitlines()
        records = [strict_json_loads(line) for line in lines]
    except (UnicodeDecodeError, StrictJSONError) as exc:
        raise SignBundleTransactionError("staged rotation audit delta is not valid JSONL") from exc
    if len(records) != 1 or not isinstance(records[0], dict) or source_file is None:
        raise SignBundleTransactionError(f"rotation {action.key!r} must append exactly one staged audit record")
    record = cast("dict[str, Any]", records[0])
    expected_rotation = {
        "source_file": source_file,
        "old_key": action.key,
        "new_key": expected_key,
    }
    expected_applied = {
        source_file: {
            "rotations_applied": 1,
            "stale_entries_removed": 0,
        }
    }
    required_fields = {
        "schema_version",
        "kind",
        "recorded_at",
        "allowlist_dir",
        "rotations",
        "stale_entries_removed",
        "applied",
    }
    recorded_allowlist = record.get("allowlist_dir")
    recorded_allowlist_is_candidate = (
        isinstance(recorded_allowlist, str) and Path(recorded_allowlist).resolve().parent.parent == tx_path.resolve()
    )
    if (
        set(record) != required_fields
        or record.get("schema_version") != 1
        or record.get("kind") != "tier_model_rotation"
        or not _is_timezone_aware_iso8601(record.get("recorded_at"))
        or not recorded_allowlist_is_candidate
        or record.get("rotations") != [expected_rotation]
        or record.get("stale_entries_removed") != []
        or record.get("applied") != expected_applied
    ):
        raise SignBundleTransactionError(f"rotation {action.key!r} appended an unrelated staged audit record")


def _is_timezone_aware_iso8601(value: Any) -> bool:
    from datetime import datetime

    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() is not None


def _load_unique_yaml_mapping(
    payload: bytes,
    *,
    error_context: str,
) -> dict[str, Any]:
    """Parse one YAML mapping while rejecting duplicate keys at every depth."""
    import yaml

    class UniqueKeySafeLoader(yaml.SafeLoader):
        """SafeLoader variant that rejects duplicate keys at every depth."""

    def construct_unique_mapping(
        loader: Any,
        node: Any,
        deep: bool = False,
    ) -> dict[Any, Any]:
        loader.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable mapping key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeySafeLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_unique_mapping,
    )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SignBundleTransactionError(f"{error_context} is not valid UTF-8 YAML") from exc
    try:
        raw = yaml.load(text, Loader=UniqueKeySafeLoader) if text else {}
    except yaml.YAMLError as exc:
        raise SignBundleTransactionError(f"{error_context} is not valid YAML") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SignBundleTransactionError(f"{error_context} must be a YAML mapping")
    return cast("dict[str, Any]", raw)


def _prepare_completed_judge_evidence(
    action: Any,
    *,
    tx_path: Path,
    candidate_dir: Path,
    source_file: str | None,
    expected_key: str,
) -> None:
    """Ensure a completed signed action has its exact append-only audit event."""
    if action.kind not in {"justify", "drift_repair"}:
        return
    if source_file is None:
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} has no verified owning YAML")

    _assert_target_yaml_transition(
        action,
        tx_path=tx_path,
        candidate_dir=candidate_dir,
        source_file=source_file,
        expected_key=expected_key,
        success=True,
    )
    if not candidate_dir.name.startswith("enforce_"):
        return
    payload = _signed_judge_event_payload(
        action,
        tx_path=tx_path,
        candidate_dir=candidate_dir,
        source_file=source_file,
        expected_key=expected_key,
    )

    checkpoint = tx_path / "checkpoint"
    metadata = strict_json_loads((checkpoint / "metadata.json").read_text(encoding="utf-8"))
    saved = checkpoint / "judge-decision-events.jsonl"
    if metadata.get("judge_events_existed") is True and not saved.is_file():
        raise SignBundleTransactionError("judge-event checkpoint is missing its before-image")
    before = saved.read_bytes() if metadata.get("judge_events_existed") is True else b""
    event_path = candidate_dir / ".judge-metrics" / "judge-decision-events.jsonl"
    after = event_path.read_bytes() if event_path.is_file() else b""
    if not after.startswith(before):
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} rewrote prior judge decision events")
    if after[len(before) :]:
        return
    rendered = json.dumps(payload, sort_keys=True) + "\n"

    def append_to_exact_before(existing: str | None) -> str:
        current = (existing or "").encode("utf-8")
        if current != before:
            raise SignBundleTransactionError(f"{action.kind} {action.key!r} decision events changed while reconstructing recovery evidence")
        return (existing or "") + rendered

    atomic_update_text(
        event_path,
        append_to_exact_before,
        encoding="utf-8",
        create_parent=True,
    )


def _signed_judge_event_payload(
    action: Any,
    *,
    tx_path: Path,
    candidate_dir: Path,
    source_file: str | None,
    expected_key: str,
) -> dict[str, Any]:
    """Derive the only valid success event from signed YAML and pinned policy."""
    if source_file is None:
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} has no verified owning YAML")
    target = candidate_dir / _local_allowlist_filename(source_file)
    mapping = _load_unique_yaml_mapping(
        target.read_bytes() if target.is_file() else b"",
        error_context=f"{action.kind} {action.key!r} candidate YAML",
    )
    entries = mapping.get("allow_hits", [])
    if not isinstance(entries, list):
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} requires list-valued allow_hits in {source_file}")
    matching = [entry for entry in entries if isinstance(entry, dict) and entry.get("key") == expected_key]
    if len(matching) != 1:
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} cannot bind evidence without one expected entry")
    entry = cast("dict[str, Any]", matching[0])

    signing_policy = load_manifest(tx_path).get("signing_policy")
    operator_override = signing_policy.get("operator_override") if isinstance(signing_policy, dict) else None
    if not isinstance(operator_override, bool):
        raise SignBundleTransactionError("transaction signing policy operator_override must be a boolean")

    effective_verdict = entry.get("judge_verdict")
    stored_model_verdict = entry.get("judge_model_verdict")
    if not operator_override and effective_verdict == "ACCEPTED" and stored_model_verdict is None:
        model_verdict = "ACCEPTED"
    elif operator_override and effective_verdict == "OVERRIDDEN_BY_OPERATOR" and stored_model_verdict in {"ACCEPTED", "BLOCKED"}:
        model_verdict = stored_model_verdict
    else:
        raise SignBundleTransactionError(
            f"{action.kind} {action.key!r} signed verdict metadata contradicts the pinned operator-override policy"
        )

    recorded_at = entry.get("judge_recorded_at")
    key_parts = expected_key.split(":")
    if len(key_parts) < 2 or not _is_timezone_aware_iso8601(recorded_at):
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} has signed metadata that cannot bind its decision event")
    return {
        "schema_version": 1,
        "source_file": key_parts[0],
        "entry_key": expected_key,
        "rule_id": key_parts[1],
        "effective_verdict": effective_verdict,
        "model_verdict": model_verdict,
        "recorded_at": recorded_at,
        "write_disposition": "written",
    }


def _assert_target_yaml_transition(
    action: Any,
    *,
    tx_path: Path,
    candidate_dir: Path,
    source_file: str,
    expected_key: str,
    success: bool,
) -> None:
    """Compare action-owned YAML semantically while preserving every sibling."""
    checkpoint = tx_path / "checkpoint"
    metadata = strict_json_loads((checkpoint / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("relative_path") != source_file:
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} checkpoint target does not match verified owner {source_file!r}")
    if metadata.get("existed") is True and not (checkpoint / "allowlist-file").is_file():
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} checkpoint is missing its owning YAML before-image")
    before_bytes = (checkpoint / "allowlist-file").read_bytes() if metadata.get("existed") is True else b""
    target = candidate_dir / _local_allowlist_filename(source_file)
    after_bytes = target.read_bytes() if target.is_file() else b""

    before_mapping = _load_unique_yaml_mapping(
        before_bytes,
        error_context=f"{action.kind} {action.key!r} checkpoint YAML",
    )
    after_mapping = _load_unique_yaml_mapping(
        after_bytes,
        error_context=f"{action.kind} {action.key!r} candidate YAML",
    )
    before_other = {key: value for key, value in before_mapping.items() if key != "allow_hits"}
    after_other = {key: value for key, value in after_mapping.items() if key != "allow_hits"}
    if before_other != after_other:
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} changed unrelated YAML sections in {source_file}")
    before_entries = before_mapping.get("allow_hits", [])
    after_entries = after_mapping.get("allow_hits", [])
    if before_entries is None:
        before_entries = []
    if after_entries is None:
        after_entries = []
    if not isinstance(before_entries, list) or not isinstance(after_entries, list):
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} requires list-valued allow_hits in {source_file}")
    if not success:
        if before_entries != after_entries:
            raise SignBundleTransactionError(f"{action.kind} {action.key!r} failed after changing allow_hits in {source_file}")
        return

    if action.kind == "justify":
        matches = [index for index, entry in enumerate(after_entries) if isinstance(entry, dict) and entry.get("key") == expected_key]
        if len(matches) != 1:
            raise SignBundleTransactionError(f"justify {action.key!r} did not add exactly one expected entry")
        index = matches[0]
        if after_entries[:index] + after_entries[index + 1 :] != before_entries:
            raise SignBundleTransactionError(f"justify {action.key!r} changed unrelated allow_hits entries")
        return

    before_matches = [index for index, entry in enumerate(before_entries) if isinstance(entry, dict) and entry.get("key") == action.key]
    if len(before_matches) != 1:
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} checkpoint lacks one unique source entry")
    index = before_matches[0]
    if action.kind == "stale_delete":
        if after_entries != before_entries[:index] + before_entries[index + 1 :]:
            raise SignBundleTransactionError(f"stale_delete {action.key!r} changed unrelated allow_hits entries")
        return

    after_matches = [
        candidate_index
        for candidate_index, entry in enumerate(after_entries)
        if isinstance(entry, dict) and entry.get("key") == expected_key
    ]
    if len(after_matches) != 1:
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} did not produce one expected replacement entry")
    after_index = after_matches[0]
    if index != after_index:
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} moved its replacement entry")
    if before_entries[:index] != after_entries[:index] or before_entries[index + 1 :] != after_entries[index + 1 :]:
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} changed unrelated allow_hits entries")
    if action.kind == "rotation":
        before_entry = cast("dict[str, Any]", before_entries[index])
        after_entry = cast("dict[str, Any]", after_entries[index])
        expected_entry = dict(before_entry)
        expected_entry["key"] = expected_key
        if after_entry != expected_entry:
            raise SignBundleTransactionError(f"rotation {action.key!r} changed fields beyond the verified key transition")


def _allow_hits_keys(allowlist_dir: Path, source_file: str | None = None) -> list[str]:
    import yaml

    paths = [allowlist_dir / _local_allowlist_filename(source_file)] if source_file is not None else sorted(allowlist_dir.glob("*.yaml"))
    keys: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"cannot inspect transaction YAML {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"transaction YAML must be a mapping: {path}")
        entries = raw.get("allow_hits", [])
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise ValueError(f"transaction YAML allow_hits must be a list: {path}")
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                keys.append(entry["key"])
    return keys


def _action_target_key(action: Any, *, repair_keys_by_stale_key: dict[str, str]) -> str:
    if action.kind == "drift_repair":
        return repair_keys_by_stale_key.get(cast("str", action.key), cast("str", action.key))
    return cast("str", action.key)


def _action_is_complete(
    action: Any,
    *,
    verification: Any,
    repair_keys_by_stale_key: dict[str, str],
    args: argparse.Namespace,
    tx_path: Path,
    authoritative: bool,
) -> bool:
    try:
        if action.kind in {"justify", "drift_repair"}:
            target_key = _action_target_key(
                action,
                repair_keys_by_stale_key=repair_keys_by_stale_key,
            )
            if target_key not in _allow_hits_keys(args.allowlist_dir):
                return False
            if not authoritative:
                return True
            from elspeth_lints.core.judge_signature_diagnosis import diagnose_judge_signatures

            report = diagnose_judge_signatures(root=args.root, allowlist_dir=args.allowlist_dir)
            return any(item.key == target_key and item.status == "OK_AUTHORITATIVE" for item in report.items)
        if action.kind == "stale_delete":
            source_file = _action_source_file(
                action,
                verification=verification,
                specs_by_stale_key={},
            )
            return action.key not in _allow_hits_keys(args.allowlist_dir, source_file)
        if action.kind == "rotation":
            if verification.rotation_plan is None:
                return False
            matching = [rotation for rotation in verification.rotation_plan.rotations if rotation.old_key == action.key]
            if len(matching) != 1:
                return False
            keys = _allow_hits_keys(args.allowlist_dir, matching[0].entry_source_file)
            return (
                action.key not in keys
                and matching[0].new_key in keys
                and _rotation_audit_recorded(
                    tx_path,
                    source_file=matching[0].entry_source_file,
                    old_key=action.key,
                    new_key=matching[0].new_key,
                )
            )
    except ValueError:
        return False
    return False


def _verify_completed_actions(
    bundle: Any,
    *,
    completed: set[int],
    verification: Any,
    repair_keys_by_stale_key: dict[str, str],
    args: argparse.Namespace,
    tx_path: Path,
) -> None:
    for index in sorted(completed):
        if index < 0 or index >= len(bundle.actions):
            raise SignBundleTransactionError(f"completed action index is out of range: {index}")
    if not completed:
        return

    from elspeth_lints.core.judge_signature_diagnosis import diagnose_judge_signatures

    judge_status_by_key: dict[str, str] = {}
    if any(bundle.actions[index].kind in {"justify", "drift_repair"} for index in completed):
        report = diagnose_judge_signatures(root=args.root, allowlist_dir=args.allowlist_dir)
        judge_status_by_key = {item.key: item.status for item in report.items}
    for index in sorted(completed):
        action = bundle.actions[index]
        if action.kind in {"justify", "drift_repair"}:
            target_key = _action_target_key(
                action,
                repair_keys_by_stale_key=repair_keys_by_stale_key,
            )
            if judge_status_by_key.get(target_key) != "OK_AUTHORITATIVE":
                raise SignBundleTransactionError(f"previously produced signature no longer verifies for {target_key!r}")
        elif not _action_is_complete(
            action,
            verification=verification,
            repair_keys_by_stale_key=repair_keys_by_stale_key,
            args=args,
            tx_path=tx_path,
            authoritative=False,
        ):
            raise SignBundleTransactionError(f"recorded deterministic action no longer verifies: {action.kind} {action.key}")


def _assert_final_rotation_evidence(
    bundle: Any,
    *,
    completed: set[int],
    verification: Any,
    tx_path: Path,
) -> None:
    """Require the staged audit delta to equal completed rotation actions exactly."""
    expected: list[dict[str, str]] = []
    for index in sorted(completed):
        action = bundle.actions[index]
        if action.kind != "rotation":
            continue
        if verification.rotation_plan is None:
            raise SignBundleTransactionError("completed rotation has no verified rotation plan")
        matching = [rotation for rotation in verification.rotation_plan.rotations if rotation.old_key == action.key]
        if len(matching) != 1:
            raise SignBundleTransactionError(f"rotation {action.key!r} has {len(matching)} verified audit owners")
        expected.append(
            {
                "source_file": matching[0].entry_source_file,
                "old_key": action.key,
                "new_key": matching[0].new_key,
            }
        )

    base = (tx_path / "rotation-base.bin").read_bytes()
    staged = (tx_path / "rotation-staged.log").read_bytes()
    if not staged.startswith(base):
        raise SignBundleTransactionError("transaction rotation log does not extend its recorded base")
    try:
        lines = staged[len(base) :].decode("utf-8").splitlines()
        records = [strict_json_loads(line) for line in lines if line.strip()]
    except (UnicodeDecodeError, StrictJSONError) as exc:
        raise SignBundleTransactionError("transaction staged rotation audit is not valid JSONL") from exc
    if len(records) != len(expected):
        raise SignBundleTransactionError("transaction staged rotation audit does not exactly match completed rotations")
    actual: list[dict[str, str]] = []
    for record in records:
        if (
            not isinstance(record, dict)
            or record.get("kind") != "tier_model_rotation"
            or not isinstance(record.get("rotations"), list)
            or len(record["rotations"]) != 1
            or not isinstance(record["rotations"][0], dict)
            or record.get("stale_entries_removed") != []
        ):
            raise SignBundleTransactionError("transaction staged rotation audit contains an unrelated record")
        rotation = record["rotations"][0]
        if set(rotation) != {"source_file", "old_key", "new_key"} or not all(isinstance(value, str) for value in rotation.values()):
            raise SignBundleTransactionError("transaction staged rotation audit contains malformed rotation evidence")
        actual.append(cast("dict[str, str]", rotation))
    if actual != expected:
        raise SignBundleTransactionError("transaction staged rotation audit does not exactly match completed rotations")


def _local_allowlist_filename(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or len(path.parts) != 1 or path.name != value:
        raise SignBundleTransactionError(f"verified allowlist source_file must be one local filename; got {value!r}")
    return value


def _rotation_audit_recorded(
    tx_path: Path,
    *,
    source_file: str,
    old_key: str,
    new_key: str,
) -> bool:
    base = (tx_path / "rotation-base.bin").read_bytes()
    staged = (tx_path / "rotation-staged.log").read_bytes()
    if not staged.startswith(base):
        return False
    expected = {
        "source_file": source_file,
        "old_key": old_key,
        "new_key": new_key,
    }
    try:
        records = [strict_json_loads(line) for line in staged[len(base) :].decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, StrictJSONError):
        return False
    return any(
        isinstance(record, dict) and isinstance(record.get("rotations"), list) and expected in record["rotations"] for record in records
    )


def _canonical_rotation_delta(
    delta: bytes,
    *,
    allowlist_dir: Path,
    publish_started_at: str,
) -> bytes:
    """Validate staged JSONL and rewrite scratch provenance to the active path."""
    if not delta:
        return b""
    try:
        raw_lines = delta.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise SignBundleTransactionError("transaction rotation audit delta is not UTF-8") from exc
    rendered: list[str] = []
    for line in raw_lines:
        if not line.strip():
            continue
        try:
            record = strict_json_loads(line)
        except StrictJSONError as exc:
            raise SignBundleTransactionError("transaction rotation audit delta is not valid JSONL") from exc
        if not isinstance(record, dict) or record.get("kind") != "tier_model_rotation":
            raise SignBundleTransactionError("transaction rotation audit delta has an unexpected record kind")
        record["allowlist_dir"] = str(allowlist_dir.resolve())
        record["recorded_at"] = publish_started_at
        rendered.append(json.dumps(record, sort_keys=True, separators=(",", ":")))
    return (("\n".join(rendered) + "\n") if rendered else "").encode("utf-8")


def _rename_exchange(source: Path, destination: Path) -> None:
    if _RENAMEAT2 is None:
        raise SignBundleTransactionError("coherent sign-bundle publish requires Linux renameat2(RENAME_EXCHANGE)")
    result = _RENAMEAT2(
        ctypes.c_int(_AT_FDCWD),
        ctypes.c_char_p(os.fsencode(source)),
        ctypes.c_int(_AT_FDCWD),
        ctypes.c_char_p(os.fsencode(destination)),
        ctypes.c_uint(_RENAME_EXCHANGE),
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
            raise SignBundleTransactionError("coherent sign-bundle publish requires filesystem support for renameat2(RENAME_EXCHANGE)")
        raise SignBundleTransactionError(f"atomic allowlist directory exchange failed: {os.strerror(error_number)}")


def _probe_exchange_support(parent: Path) -> None:
    first = parent / ".exchange-probe-a"
    second = parent / ".exchange-probe-b"
    first.mkdir()
    second.mkdir()
    try:
        _rename_exchange(first, second)
        _rename_exchange(first, second)
    finally:
        first.rmdir()
        second.rmdir()


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
