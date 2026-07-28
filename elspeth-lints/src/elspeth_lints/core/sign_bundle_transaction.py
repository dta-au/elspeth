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
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from elspeth_lints.core.atomic_io import atomic_update_text

SCHEMA_VERSION = 1
TRANSACTION_DIRNAME = ".sign-bundle-transactions"
MANIFEST_NAME = "transaction.json"
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
    base_snapshot = tree_snapshot(active)

    tx_root = transaction_root(active)
    tx_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", bundle_id).strip(".-") or "bundle"
    tx_path = Path(tempfile.mkdtemp(prefix=f"{safe_id}-", dir=tx_root))
    _probe_exchange_support(tx_path)
    candidate_parent = tx_path / "candidate"
    candidate_parent.mkdir()
    candidate = candidate_parent / active.name
    shutil.copytree(active, candidate)

    rotation_base = tx_path / "rotation-base.bin"
    rotation_staged = tx_path / "rotation-staged.log"
    base_rotation_bytes = rotation_resolved.read_bytes() if rotation_resolved.is_file() else b""
    rotation_base.write_bytes(base_rotation_bytes)
    rotation_staged.write_bytes(base_rotation_bytes)

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "bundle_path": str(bundle_resolved),
        "bundle_sha256": file_sha256(bundle_resolved),
        "root": str(source_root),
        "allowlist_dir": str(active),
        "candidate_dir": str(candidate),
        "rotation_log": str(rotation_resolved),
        "signing_policy": signing_policy,
        "base_snapshot": base_snapshot,
        "candidate_snapshot": tree_snapshot(candidate),
        "completed_actions": [],
        "running_action": None,
    }
    save_manifest(tx_path, manifest)
    return tx_path, manifest


def load_manifest(tx_path: Path) -> dict[str, Any]:
    resolved = tx_path.resolve()
    manifest_path = resolved / MANIFEST_NAME
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SignBundleTransactionError(f"cannot read transaction manifest {manifest_path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise SignBundleTransactionError(f"unsupported sign-bundle transaction manifest: {manifest_path}")
    required_strings = ("bundle_path", "bundle_sha256", "root", "allowlist_dir", "candidate_dir", "rotation_log")
    for key in required_strings:
        if not isinstance(raw.get(key), str):
            raise SignBundleTransactionError(f"transaction manifest field {key!r} must be a string")
    if not isinstance(raw.get("base_snapshot"), dict) or not isinstance(raw.get("candidate_snapshot"), dict):
        raise SignBundleTransactionError("transaction manifest snapshots must be mappings")
    if not isinstance(raw.get("signing_policy"), dict):
        raise SignBundleTransactionError("transaction manifest signing_policy must be a mapping")
    if not isinstance(raw.get("completed_actions"), list):
        raise SignBundleTransactionError("transaction manifest completed_actions must be a list")
    candidate = Path(raw["candidate_dir"]).resolve()
    if candidate.parent.parent != resolved:
        raise SignBundleTransactionError("transaction candidate path escapes its transaction directory")
    return raw


def save_manifest(tx_path: Path, manifest: dict[str, Any]) -> None:
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
    active_snapshot = tree_snapshot(Path(manifest["allowlist_dir"]))
    candidate_snapshot = tree_snapshot(Path(manifest["candidate_dir"]))
    if active_snapshot == manifest["base_snapshot"] and (
        candidate_snapshot == manifest["candidate_snapshot"] or manifest.get("running_action") is not None
    ):
        return "not_published"
    if active_snapshot == manifest["candidate_snapshot"] and candidate_snapshot == manifest["base_snapshot"]:
        return "published"
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
    (checkpoint / "metadata.json").write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")


def restore_action_checkpoint(tx_path: Path, manifest: dict[str, Any]) -> None:
    checkpoint = tx_path / "checkpoint"
    metadata_path = checkpoint / "metadata.json"
    if not metadata_path.is_file():
        return
    raw = json.loads(metadata_path.read_text(encoding="utf-8"))
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


def mark_candidate_snapshot(manifest: dict[str, Any]) -> None:
    manifest["candidate_snapshot"] = tree_snapshot(Path(manifest["candidate_dir"]))


def publish_candidate(tx_path: Path, manifest: dict[str, Any]) -> None:
    """Atomically exchange the coherent candidate with the active directory."""
    active = Path(manifest["allowlist_dir"])
    candidate = Path(manifest["candidate_dir"])
    candidate_snapshot = manifest["candidate_snapshot"]
    base_snapshot = manifest["base_snapshot"]

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
    candidate_args.allowlist_dir = args.allowlist_dir if disposition == "published" else Path(manifest["candidate_dir"])
    candidate_args.rotation_log = tx_path / "rotation-staged.log"
    candidate_args._defer_override_rate_counter_snapshot = True
    completed = {int(index) for index in manifest["completed_actions"]}

    if disposition == "published":
        if completed != set(range(len(bundle.actions))):
            raise SignBundleTransactionError("published transaction journal does not contain every bundle action")
        _verify_completed_actions(
            bundle,
            completed=completed,
            verification=verification,
            repair_keys_by_stale_key=repair_keys_by_stale_key,
            args=candidate_args,
            tx_path=tx_path,
        )
        finalize_rotation_log(tx_path, manifest)
        return SignBundleRunResult(0, len(completed), recovered_publish=True)

    running = manifest.get("running_action")
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
        _assert_action_scoped_candidate_changes(
            running_action,
            tx_path=tx_path,
            manifest=manifest,
            source_file=running_source_file,
            expected_key=_expected_action_key(
                running_action,
                verification=verification,
                repair_keys_by_stale_key=repair_keys_by_stale_key,
            ),
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
            _assert_action_scoped_candidate_changes(
                running_action,
                tx_path=tx_path,
                manifest=manifest,
                source_file=running_source_file,
                expected_key=_expected_action_key(
                    running_action,
                    verification=verification,
                    repair_keys_by_stale_key=repair_keys_by_stale_key,
                ),
                verify_semantics=True,
            )
            completed.add(running_index)
        else:
            restore_action_checkpoint(tx_path, manifest)
        manifest["running_action"] = None
        manifest["completed_actions"] = sorted(completed)
        mark_candidate_snapshot(manifest)
        save_manifest(tx_path, manifest)
        clear_action_checkpoint(tx_path)

    assert_candidate_unchanged(manifest)
    _verify_completed_actions(
        bundle,
        completed=completed,
        verification=verification,
        repair_keys_by_stale_key=repair_keys_by_stale_key,
        args=candidate_args,
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
        _assert_action_scoped_candidate_changes(
            action,
            tx_path=tx_path,
            manifest=manifest,
            source_file=source_file,
            expected_key=_expected_action_key(
                action,
                verification=verification,
                repair_keys_by_stale_key=repair_keys_by_stale_key,
            ),
            verify_semantics=code == 0,
        )
        if code != 0:
            manifest["running_action"] = None
            mark_candidate_snapshot(manifest)
            save_manifest(tx_path, manifest)
            clear_action_checkpoint(tx_path)
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
        mark_candidate_snapshot(manifest)
        save_manifest(tx_path, manifest)
        clear_action_checkpoint(tx_path)

    _verify_completed_actions(
        bundle,
        completed=completed,
        verification=verification,
        repair_keys_by_stale_key=repair_keys_by_stale_key,
        args=candidate_args,
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
    publish_candidate(tx_path, manifest)
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
    if target is not None and verify_semantics is not None:
        _assert_target_yaml_transition(
            action,
            tx_path=tx_path,
            candidate_dir=Path(manifest["candidate_dir"]),
            source_file=target,
            expected_key=expected_key,
            success=verify_semantics,
        )


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
    import yaml

    checkpoint = tx_path / "checkpoint"
    metadata = json.loads((checkpoint / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("relative_path") != source_file:
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} checkpoint target does not match verified owner {source_file!r}")
    if metadata.get("existed") is True and not (checkpoint / "allowlist-file").is_file():
        raise SignBundleTransactionError(f"{action.kind} {action.key!r} checkpoint is missing its owning YAML before-image")
    before_bytes = (checkpoint / "allowlist-file").read_bytes() if metadata.get("existed") is True else b""
    target = candidate_dir / _local_allowlist_filename(source_file)
    after_bytes = target.read_bytes() if target.is_file() else b""

    def load_mapping(payload: bytes, *, label: str) -> dict[str, Any]:
        try:
            raw = yaml.safe_load(payload.decode("utf-8")) if payload else {}
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise SignBundleTransactionError(f"{action.kind} {action.key!r} left invalid {label} YAML") from exc
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise SignBundleTransactionError(f"{action.kind} {action.key!r} {label} YAML must be a mapping")
        return cast("dict[str, Any]", raw)

    before_mapping = load_mapping(before_bytes, label="checkpoint")
    after_mapping = load_mapping(after_bytes, label="candidate")
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
        records = [json.loads(line) for line in staged[len(base) :].decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return any(
        isinstance(record, dict) and isinstance(record.get("rotations"), list) and expected in record["rotations"] for record in records
    )


def _canonical_rotation_delta(delta: bytes, *, allowlist_dir: Path) -> bytes:
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
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SignBundleTransactionError("transaction rotation audit delta is not valid JSONL") from exc
        if not isinstance(record, dict) or record.get("kind") != "tier_model_rotation":
            raise SignBundleTransactionError("transaction rotation audit delta has an unexpected record kind")
        record["allowlist_dir"] = str(allowlist_dir.resolve())
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
