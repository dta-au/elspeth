"""Strict, identity-bound corpus records for manual tier-model remediation.

The corpus is an editable review aid, not an authority surface. Each row
copies structural facts from a freshly staged :class:`BundleAction` only to
help a reviewer navigate the corpus. Those facts never establish producer
provenance and this module deliberately leaves both ``classification`` and
``producer_provenance`` unset for a human to fill.

Reconciliation binds the report to four independently recomputed surfaces:

* exact review-bundle bytes (bundle id + SHA-256);
* the repository Git HEAD;
* a digest of the scanned Python and allowlist inputs; and
* a digest of the freshly re-derived action inventory.

Every live action, including every ``drift_repair`` action, must have exactly
one report row. Duplicate, missing, extra, edited, or stale rows fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from elspeth_lints.core.allowlist import _JUDGE_METADATA_SIGNATURE_ENV_VAR
from elspeth_lints.core.atomic_io import AtomicWriteConflictError, atomic_update_text
from elspeth_lints.core.review_bundle import BundleAction, ReviewBundle, load_bundle
from elspeth_lints.core.source_snapshot import (
    SourceSnapshotError,
)
from elspeth_lints.core.source_snapshot import (
    current_git_head as _shared_current_git_head,
)
from elspeth_lints.core.source_snapshot import (
    source_snapshot_sha256 as _shared_source_snapshot_sha256,
)
from elspeth_lints.core.strict_json import strict_json_loads
from elspeth_lints.rules.trust_tier.tier_model.rule import iter_scannable_python_files

CLASSIFICATION_CORPUS_SCHEMA_VERSION = 1

_CORPUS_FIELDS = (
    "schema_version",
    "bundle_id",
    "bundle_sha256",
    "git_head",
    "source_snapshot_sha256",
    "live_scan_sha256",
    "entries",
)
_ENTRY_FIELDS = (
    "action_key",
    "action_kind",
    "action_lane",
    "file_path",
    "symbol",
    "rule",
    "fingerprint",
    "scope_fingerprint",
    "ast_path",
    "diagnosis_status",
    "source_file",
    "classification",
    "producer_provenance",
    "notes",
)
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_HEX_GIT_HEAD = re.compile(r"[0-9a-f]{40,64}")


class CorpusReconciliationError(ValueError):
    """The corpus does not reconcile one-to-one with the staged actions."""


class CorpusStaleError(CorpusReconciliationError):
    """A bound bundle, source, revision, or live-scan identity has changed."""


@dataclass(frozen=True, slots=True)
class ClassificationEntry:
    """One human-editable classification row plus advisory structural facts."""

    action_key: str
    action_kind: str
    action_lane: str
    file_path: str | None
    symbol: str | None
    rule: str | None
    fingerprint: str | None
    scope_fingerprint: str | None
    ast_path: str | None
    diagnosis_status: str | None
    source_file: str | None
    classification: str | None = None
    producer_provenance: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        _validate_nonempty_string(self.action_key, "ClassificationEntry.action_key")
        _validate_nonempty_string(self.action_kind, "ClassificationEntry.action_kind")
        _validate_nonempty_string(self.action_lane, "ClassificationEntry.action_lane")
        _validate_optional_string(self.file_path, "ClassificationEntry.file_path")
        _validate_optional_string(self.symbol, "ClassificationEntry.symbol")
        _validate_optional_string(self.rule, "ClassificationEntry.rule")
        _validate_optional_string(self.fingerprint, "ClassificationEntry.fingerprint")
        _validate_optional_string(self.scope_fingerprint, "ClassificationEntry.scope_fingerprint")
        _validate_optional_string(self.ast_path, "ClassificationEntry.ast_path")
        _validate_optional_string(self.diagnosis_status, "ClassificationEntry.diagnosis_status")
        _validate_optional_string(self.source_file, "ClassificationEntry.source_file")
        _validate_optional_string(self.classification, "ClassificationEntry.classification")
        _validate_optional_string(self.producer_provenance, "ClassificationEntry.producer_provenance")
        _validate_optional_string(self.notes, "ClassificationEntry.notes")


@dataclass(frozen=True, slots=True)
class ClassificationCorpus:
    """An exact action inventory bound to the source snapshot it classifies."""

    schema_version: int
    bundle_id: str
    bundle_sha256: str
    git_head: str
    source_snapshot_sha256: str
    live_scan_sha256: str
    entries: tuple[ClassificationEntry, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != CLASSIFICATION_CORPUS_SCHEMA_VERSION:
            raise ValueError(
                f"classification corpus schema_version={self.schema_version!r}; "
                f"this build understands {CLASSIFICATION_CORPUS_SCHEMA_VERSION}"
            )
        _validate_nonempty_string(self.bundle_id, "ClassificationCorpus.bundle_id")
        _validate_sha256(self.bundle_sha256, "ClassificationCorpus.bundle_sha256")
        _validate_git_head(self.git_head)
        _validate_sha256(self.source_snapshot_sha256, "ClassificationCorpus.source_snapshot_sha256")
        _validate_sha256(self.live_scan_sha256, "ClassificationCorpus.live_scan_sha256")
        if not isinstance(self.entries, tuple):
            raise ValueError(f"ClassificationCorpus.entries must be a tuple; got {type(self.entries).__name__}")
        seen: set[str] = set()
        for entry in self.entries:
            if not isinstance(entry, ClassificationEntry):
                raise ValueError(f"ClassificationCorpus.entries must contain ClassificationEntry; got {type(entry).__name__}")
            if entry.action_key in seen:
                raise CorpusReconciliationError(f"classification corpus contains duplicate action row {entry.action_key!r}")
            seen.add(entry.action_key)


@dataclass(frozen=True, slots=True)
class CorpusSummary:
    """Counts from a successful exact reconciliation."""

    actions_total: int
    drift_actions_total: int
    classified_total: int


@dataclass(frozen=True, slots=True)
class _LiveCorpusState:
    """One stable observation of the revision, inputs, and derived actions."""

    git_head: str
    source_snapshot_sha256: str
    actions: tuple[BundleAction, ...]


def action_scan_sha256(actions: Iterable[BundleAction]) -> str:
    """Hash a deterministic, order-independent live-action inventory."""
    action_map = _actions_by_key(actions, context="live scan")
    payload = [_action_structural_payload(action_map[key]) for key in sorted(action_map)]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_snapshot_sha256(*, source_root: Path, allowlist_dir: Path) -> str:
    """Re-export the shared scanner-input digest for corpus callers."""
    return _shared_source_snapshot_sha256(
        source_root=source_root,
        allowlist_dir=allowlist_dir,
        _iter_python_files=iter_scannable_python_files,
    )


def current_git_head(repo_root: Path) -> str:
    """Re-export shared HEAD capture with the corpus-specific stale error."""
    try:
        return _shared_current_git_head(repo_root)
    except SourceSnapshotError as exc:
        raise CorpusStaleError(f"cannot bind classification corpus to Git HEAD: {exc}") from exc


def live_stage_scan_actions(*, root: Path, allowlist_dir: Path) -> tuple[BundleAction, ...]:
    """Re-derive the exact key-free action inventory used by ``stage_scan``."""
    from elspeth_lints.mcp.server import build_scan_actions

    actions = build_scan_actions(root=Path(root), allowlist_dir=Path(allowlist_dir))
    return tuple(_require_bundle_action(action, context="stage scan") for action in actions)


def create_classification_corpus(
    *,
    bundle_path: Path,
    git_head: str,
    source_snapshot_sha256_value: str,
    live_actions: Iterable[BundleAction],
) -> ClassificationCorpus:
    """Create an unclassified corpus after proving the bundle is live."""
    _validate_git_head(git_head)
    _validate_sha256(source_snapshot_sha256_value, "source_snapshot_sha256_value")
    bundle_bytes, bundle = _read_bundle_with_bytes(bundle_path)
    live_action_map = _actions_by_key(live_actions, context="live scan")
    bundle_action_map = _actions_by_key(bundle.actions, context="review bundle")
    _require_same_actions(bundle_action_map, live_action_map)

    entries = tuple(_entry_from_action(live_action_map[key]) for key in sorted(live_action_map))
    return ClassificationCorpus(
        schema_version=CLASSIFICATION_CORPUS_SCHEMA_VERSION,
        bundle_id=bundle.bundle_id,
        bundle_sha256=hashlib.sha256(bundle_bytes).hexdigest(),
        git_head=git_head,
        source_snapshot_sha256=source_snapshot_sha256_value,
        live_scan_sha256=action_scan_sha256(live_action_map.values()),
        entries=entries,
    )


def verify_classification_corpus(
    *,
    corpus: ClassificationCorpus,
    bundle_path: Path,
    git_head: str,
    source_snapshot_sha256_value: str,
    live_actions: Iterable[BundleAction],
    require_classified: bool = False,
) -> CorpusSummary:
    """Recompute every binding and reconcile every row exactly once."""
    if not isinstance(corpus, ClassificationCorpus):
        raise ValueError(f"corpus must be ClassificationCorpus; got {type(corpus).__name__}")
    if not isinstance(require_classified, bool):
        raise ValueError(f"require_classified must be bool; got {type(require_classified).__name__}")
    _validate_git_head(git_head)
    _validate_sha256(source_snapshot_sha256_value, "source_snapshot_sha256_value")

    bundle_bytes, bundle = _read_bundle_with_bytes(bundle_path)
    bundle_sha256 = hashlib.sha256(bundle_bytes).hexdigest()
    if corpus.bundle_id != bundle.bundle_id:
        raise CorpusStaleError(f"classification corpus bundle_id={corpus.bundle_id!r} does not match live bundle {bundle.bundle_id!r}")
    if corpus.bundle_sha256 != bundle_sha256:
        raise CorpusStaleError("classification corpus bundle SHA-256 is stale")
    if corpus.git_head != git_head:
        raise CorpusStaleError("classification corpus Git HEAD is stale")
    if corpus.source_snapshot_sha256 != source_snapshot_sha256_value:
        raise CorpusStaleError("classification corpus source snapshot SHA-256 is stale")

    live_action_map = _actions_by_key(live_actions, context="live scan")
    live_scan_sha256 = action_scan_sha256(live_action_map.values())
    if corpus.live_scan_sha256 != live_scan_sha256:
        raise CorpusStaleError("classification corpus live scan SHA-256 is stale")
    bundle_action_map = _actions_by_key(bundle.actions, context="review bundle")
    _require_same_actions(bundle_action_map, live_action_map)

    row_map = {entry.action_key: entry for entry in corpus.entries}
    expected_keys = set(live_action_map)
    actual_keys = set(row_map)
    missing = sorted(expected_keys - actual_keys)
    extra = sorted(actual_keys - expected_keys)
    if missing or extra:
        raise CorpusReconciliationError(f"classification corpus action rows do not reconcile: missing={missing!r}, extra={extra!r}")

    classified_total = 0
    drift_actions_total = 0
    incomplete: list[str] = []
    for key in sorted(expected_keys):
        action = live_action_map[key]
        entry = row_map[key]
        if _entry_structural_payload(entry) != _action_structural_payload(action):
            raise CorpusReconciliationError(f"classification corpus structural facts are stale or edited for {key!r}")
        if action.kind == "drift_repair":
            drift_actions_total += 1
        if entry.classification is None or entry.producer_provenance is None:
            incomplete.append(key)
        else:
            classified_total += 1
    if require_classified and incomplete:
        raise CorpusReconciliationError(
            f"classification corpus has unclassified or missing producer provenance action rows: {incomplete!r}"
        )

    return CorpusSummary(
        actions_total=len(expected_keys),
        drift_actions_total=drift_actions_total,
        classified_total=classified_total,
    )


def dump_classification_corpus(corpus: ClassificationCorpus) -> str:
    """Serialize the corpus deterministically without source or allowlist bytes."""
    entries = sorted(corpus.entries, key=lambda entry: entry.action_key)
    payload = {
        "schema_version": corpus.schema_version,
        "bundle_id": corpus.bundle_id,
        "bundle_sha256": corpus.bundle_sha256,
        "git_head": corpus.git_head,
        "source_snapshot_sha256": corpus.source_snapshot_sha256,
        "live_scan_sha256": corpus.live_scan_sha256,
        "entries": [_entry_to_dict(entry) for entry in entries],
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def load_classification_corpus(text: str) -> ClassificationCorpus:
    """Strictly load an editable classification corpus."""
    data = strict_json_loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"classification corpus must be a JSON object; got {type(data).__name__}")
    _reject_unknown_keys(data, _CORPUS_FIELDS, "corpus")
    schema_version = _require(data, "schema_version", "corpus")
    if type(schema_version) is not int or schema_version != CLASSIFICATION_CORPUS_SCHEMA_VERSION:
        raise ValueError(
            f"classification corpus schema_version={schema_version!r}; this build understands {CLASSIFICATION_CORPUS_SCHEMA_VERSION}"
        )
    raw_entries = _require(data, "entries", "corpus")
    if not isinstance(raw_entries, list):
        raise ValueError(f"classification corpus entries must be a list; got {type(raw_entries).__name__}")
    entries = tuple(sorted((_entry_from_dict(item) for item in raw_entries), key=lambda entry: entry.action_key))
    return ClassificationCorpus(
        schema_version=schema_version,
        bundle_id=_require_string(data, "bundle_id", "corpus"),
        bundle_sha256=_require_string(data, "bundle_sha256", "corpus"),
        git_head=_require_string(data, "git_head", "corpus"),
        source_snapshot_sha256=_require_string(data, "source_snapshot_sha256", "corpus"),
        live_scan_sha256=_require_string(data, "live_scan_sha256", "corpus"),
        entries=entries,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Create or verify a key-free, source-bound manual review corpus."""
    parser = _build_cli_parser()
    args = parser.parse_args(argv)
    try:
        _assert_key_free()
        repo_root = Path(args.repo_root).resolve()
        root = _resolve_repo_directory(repo_root=repo_root, value=Path(args.root), label="--root")
        allowlist_dir = _resolve_repo_directory(
            repo_root=repo_root,
            value=Path(args.allowlist_dir),
            label="--allowlist-dir",
        )
        bundle_path = _resolve_repo_file(
            repo_root=repo_root,
            value=Path(args.bundle),
            label="bundle",
        )
        live_state = _capture_stable_live_state(
            repo_root=repo_root,
            root=root,
            allowlist_dir=allowlist_dir,
        )
        if args.command == "create":
            report_path = _resolve_report_path(repo_root=repo_root, value=Path(args.output))
            corpus = create_classification_corpus(
                bundle_path=bundle_path,
                git_head=live_state.git_head,
                source_snapshot_sha256_value=live_state.source_snapshot_sha256,
                live_actions=live_state.actions,
            )
            summary = verify_classification_corpus(
                corpus=corpus,
                bundle_path=bundle_path,
                git_head=live_state.git_head,
                source_snapshot_sha256_value=live_state.source_snapshot_sha256,
                live_actions=live_state.actions,
            )
            _publish_report(
                report_path=report_path,
                content=dump_classification_corpus(corpus),
                overwrite=args.overwrite,
            )
            _write_summary(report_path=report_path, summary=summary)
            return 0
        if args.command == "verify":
            report_path = _resolve_report_path(repo_root=repo_root, value=Path(args.corpus))
            corpus = load_classification_corpus(report_path.read_text(encoding="utf-8"))
            summary = verify_classification_corpus(
                corpus=corpus,
                bundle_path=bundle_path,
                git_head=live_state.git_head,
                source_snapshot_sha256_value=live_state.source_snapshot_sha256,
                live_actions=live_state.actions,
                require_classified=args.require_classified,
            )
            _write_summary(report_path=report_path, summary=summary)
            return 0
        raise AssertionError(f"unhandled tier-model-corpus command {args.command!r}")
    except (AtomicWriteConflictError, OSError, ValueError) as exc:
        sys.stderr.write(f"tier-model-corpus: {exc}\n")
        return 2


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="elspeth-tier-corpus",
        description="Create and verify the key-free manual tier-model remediation corpus.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "verify"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("bundle", type=Path, help="Exact stage_scan review bundle JSON.")
        command_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
        command_parser.add_argument("--root", type=Path, default=Path("src/elspeth"))
        command_parser.add_argument(
            "--allowlist-dir",
            type=Path,
            default=Path("config/cicd/enforce_tier_model"),
        )
    create_parser = subparsers.choices["create"]
    create_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Report path under <repo>/.elspeth/tier-model-corpus/.",
    )
    create_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing report only after all live identities reconcile.",
    )
    verify_parser = subparsers.choices["verify"]
    verify_parser.add_argument(
        "--corpus",
        type=Path,
        required=True,
        help="Existing report under <repo>/.elspeth/tier-model-corpus/.",
    )
    verify_parser.add_argument(
        "--require-classified",
        action="store_true",
        help="Fail unless every action has an explicit human classification.",
    )
    return parser


def _assert_key_free() -> None:
    if _JUDGE_METADATA_SIGNATURE_ENV_VAR not in os.environ:
        return
    value = os.environ[_JUDGE_METADATA_SIGNATURE_ENV_VAR]
    if value == "":
        return
    raise CorpusReconciliationError(
        f"refusing to run while {_JUDGE_METADATA_SIGNATURE_ENV_VAR} is present; corpus classification is a key-free agent workflow"
    )


def _resolve_repo_directory(*, repo_root: Path, value: Path, label: str) -> Path:
    resolved = _resolve_repo_path(repo_root=repo_root, value=value, label=label)
    if not resolved.is_dir():
        raise ValueError(f"{label} is not a directory: {resolved}")
    return resolved


def _resolve_repo_file(*, repo_root: Path, value: Path, label: str) -> Path:
    resolved = _resolve_repo_path(repo_root=repo_root, value=value, label=label)
    if not resolved.is_file():
        raise ValueError(f"{label} is not a file: {resolved}")
    return resolved


def _resolve_repo_path(*, repo_root: Path, value: Path, label: str) -> Path:
    candidate = value
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(repo_root):
        raise ValueError(f"{label} must resolve inside --repo-root {repo_root}: {resolved}")
    return resolved


def _resolve_report_path(*, repo_root: Path, value: Path) -> Path:
    allowed_root = (repo_root / ".elspeth/tier-model-corpus").resolve()
    if not allowed_root.is_relative_to(repo_root):
        raise ValueError(f"classification report directory must resolve inside --repo-root {repo_root}: {allowed_root}")
    candidate = value
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    if resolved == allowed_root or not resolved.is_relative_to(allowed_root):
        raise ValueError(f"classification report must be a .json file under {allowed_root}; got {resolved}")
    if resolved.suffix != ".json":
        raise ValueError(f"classification report must use a .json suffix: {resolved}")
    return resolved


def _capture_stable_live_state(
    *,
    repo_root: Path,
    root: Path,
    allowlist_dir: Path,
) -> _LiveCorpusState:
    """Derive actions only while the bound revision and scanned inputs stay stable."""
    head_before = current_git_head(repo_root)
    source_before = source_snapshot_sha256(source_root=root, allowlist_dir=allowlist_dir)
    actions = live_stage_scan_actions(root=root, allowlist_dir=allowlist_dir)
    source_after = source_snapshot_sha256(source_root=root, allowlist_dir=allowlist_dir)
    head_after = current_git_head(repo_root)
    changed: list[str] = []
    if head_before != head_after:
        changed.append("Git HEAD")
    if source_before != source_after:
        changed.append("source/allowlist snapshot")
    if changed:
        raise CorpusStaleError(f"{', '.join(changed)} changed while deriving live actions")
    return _LiveCorpusState(
        git_head=head_after,
        source_snapshot_sha256=source_after,
        actions=actions,
    )


def _publish_report(*, report_path: Path, content: str, overwrite: bool) -> None:
    """Publish under one lock so the no-overwrite decision cannot race."""

    def update(current: str | None) -> str:
        if current is not None and not overwrite:
            raise CorpusReconciliationError(f"classification report already exists: {report_path}; pass --overwrite to replace it")
        return content

    report_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_update_text(report_path, update, create_parent=False)


def _write_summary(*, report_path: Path, summary: CorpusSummary) -> None:
    payload = {
        "report_path": str(report_path),
        "actions_total": summary.actions_total,
        "drift_actions_total": summary.drift_actions_total,
        "classified_total": summary.classified_total,
    }
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_bundle_with_bytes(bundle_path: Path) -> tuple[bytes, ReviewBundle]:
    bundle_bytes = Path(bundle_path).read_bytes()
    try:
        bundle_text = bundle_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("review bundle is not valid UTF-8") from exc
    return bundle_bytes, load_bundle(bundle_text)


def _actions_by_key(actions: Iterable[BundleAction], *, context: str) -> dict[str, BundleAction]:
    indexed: dict[str, BundleAction] = {}
    for action in actions:
        action = _require_bundle_action(action, context=context)
        if action.key in indexed:
            raise CorpusReconciliationError(f"{context} contains duplicate action identity {action.key!r}")
        indexed[action.key] = action
    return indexed


def _require_bundle_action(action: Any, *, context: str) -> BundleAction:
    if not isinstance(action, BundleAction):
        raise ValueError(f"{context} must contain BundleAction; got {type(action).__name__}")
    return action


def _require_same_actions(
    bundle_actions: dict[str, BundleAction],
    live_actions: dict[str, BundleAction],
) -> None:
    bundle_keys = set(bundle_actions)
    live_keys = set(live_actions)
    missing = sorted(bundle_keys - live_keys)
    extra = sorted(live_keys - bundle_keys)
    changed = sorted(
        key
        for key in bundle_keys & live_keys
        if _action_structural_payload(bundle_actions[key]) != _action_structural_payload(live_actions[key])
    )
    if missing or extra or changed:
        raise CorpusStaleError(
            f"bundle actions do not match the live scan: missing_from_live={missing!r}, extra_in_live={extra!r}, changed={changed!r}"
        )


def _entry_from_action(action: BundleAction) -> ClassificationEntry:
    return ClassificationEntry(
        action_key=action.key,
        action_kind=action.kind,
        action_lane=action.lane,
        file_path=action.file_path,
        symbol=action.symbol,
        rule=action.rule,
        fingerprint=action.fingerprint,
        scope_fingerprint=action.scope_fingerprint,
        ast_path=action.ast_path,
        diagnosis_status=action.diagnosis_status,
        source_file=action.source_file,
    )


def _action_structural_payload(action: BundleAction) -> dict[str, str | None]:
    return {
        "action_key": action.key,
        "action_kind": action.kind,
        "action_lane": action.lane,
        "file_path": action.file_path,
        "symbol": action.symbol,
        "rule": action.rule,
        "fingerprint": action.fingerprint,
        "scope_fingerprint": action.scope_fingerprint,
        "ast_path": action.ast_path,
        "diagnosis_status": action.diagnosis_status,
        "source_file": action.source_file,
    }


def _entry_structural_payload(entry: ClassificationEntry) -> dict[str, str | None]:
    return {
        "action_key": entry.action_key,
        "action_kind": entry.action_kind,
        "action_lane": entry.action_lane,
        "file_path": entry.file_path,
        "symbol": entry.symbol,
        "rule": entry.rule,
        "fingerprint": entry.fingerprint,
        "scope_fingerprint": entry.scope_fingerprint,
        "ast_path": entry.ast_path,
        "diagnosis_status": entry.diagnosis_status,
        "source_file": entry.source_file,
    }


def _entry_to_dict(entry: ClassificationEntry) -> dict[str, str | None]:
    payload = _entry_structural_payload(entry)
    payload["classification"] = entry.classification
    payload["producer_provenance"] = entry.producer_provenance
    payload["notes"] = entry.notes
    return payload


def _entry_from_dict(data: Any) -> ClassificationEntry:
    if not isinstance(data, dict):
        raise ValueError(f"classification entry must be a JSON object; got {type(data).__name__}")
    _reject_unknown_keys(data, _ENTRY_FIELDS, "entry")
    return ClassificationEntry(
        action_key=_require_string(data, "action_key", "entry"),
        action_kind=_require_string(data, "action_kind", "entry"),
        action_lane=_require_string(data, "action_lane", "entry"),
        file_path=_optional_string(data, "file_path", "entry"),
        symbol=_optional_string(data, "symbol", "entry"),
        rule=_optional_string(data, "rule", "entry"),
        fingerprint=_optional_string(data, "fingerprint", "entry"),
        scope_fingerprint=_optional_string(data, "scope_fingerprint", "entry"),
        ast_path=_optional_string(data, "ast_path", "entry"),
        diagnosis_status=_optional_string(data, "diagnosis_status", "entry"),
        source_file=_optional_string(data, "source_file", "entry"),
        classification=_optional_string(data, "classification", "entry"),
        producer_provenance=_optional_string(data, "producer_provenance", "entry"),
        notes=_optional_string(data, "notes", "entry"),
    )


def _reject_unknown_keys(data: dict[str, Any], allowed: tuple[str, ...], context: str) -> None:
    unknown = set(data) - set(allowed)
    if unknown:
        raise ValueError(f"classification {context} has unknown key(s): {sorted(unknown)!r}")


def _require(data: dict[str, Any], key: str, context: str) -> Any:
    if key not in data:
        raise ValueError(f"classification {context} is missing required field {key!r}")
    return data[key]


def _require_string(data: dict[str, Any], key: str, context: str) -> str:
    value = _require(data, key, context)
    if not isinstance(value, str) or value == "":
        raise ValueError(f"classification {context} field {key!r} must be a non-empty string; got {type(value).__name__}")
    return value


def _optional_string(data: dict[str, Any], key: str, context: str) -> str | None:
    value = _require(data, key, context)
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise ValueError(f"classification {context} field {key!r} must be a non-empty string or null; got {type(value).__name__}")
    return value


def _validate_nonempty_string(value: Any, context: str) -> None:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{context} must be a non-empty string; got {type(value).__name__}")


def _validate_optional_string(value: Any, context: str) -> None:
    if value is None:
        return
    _validate_nonempty_string(value, context)


def _validate_sha256(value: Any, context: str) -> None:
    if not isinstance(value, str) or _HEX_SHA256.fullmatch(value) is None:
        raise ValueError(f"{context} must be a lowercase 64-hex SHA-256")


def _validate_git_head(value: Any) -> None:
    if not isinstance(value, str) or _HEX_GIT_HEAD.fullmatch(value) is None:
        raise ValueError("git_head must be a lowercase 40-64 hex object id")


if __name__ == "__main__":
    raise SystemExit(main())
