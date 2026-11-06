"""Trusted baseline verification and publication; candidate execution stays remote.

The installed rule package derives the expected baseline itself. A maintenance
worker's capture and test observations cannot grant signed-gate clearance.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import stat
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from elspeth_lints.core import operator_runtime
from elspeth_lints.core.ast_walker import EXCLUDED_WALK_DIRS, EXCLUDED_WALK_PREFIXES, iter_python_files
from elspeth_lints.core.strict_json import StrictJSONError, strict_json_loads
from elspeth_lints.core.trusted_git import SourceSnapshotError, TrustedGitRepository, _open_directory, _read_file, _working_entry

BASELINE_PATH = Path("tests/unit/elspeth_lints/fixtures/fingerprint_baseline.json")
_VERIFY_MODE = "ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE"
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_FILES = 12000
_SCAN_LOCK = threading.RLock()

# Preserve the existing capture_all contract, including its explicit scan roots.
# This is deliberately not the broader registry inventory.
RULE_ROOTS: tuple[tuple[str, str], ...] = (
    ("audit_evidence.guard_symmetry", "src/elspeth"),
    ("audit_evidence.gve_attribution", "src/elspeth"),
    ("audit_evidence.nominal_base", "src/elspeth"),
    ("audit_evidence.tier_1_decoration", "src/elspeth"),
    ("composer.catch_order", "src/elspeth"),
    ("composer.exception_channel", "src/elspeth"),
    ("contract_invariants.adapter_method_budget", "src/elspeth"),
    ("contract_invariants.portable_sqlite_insert", "src/elspeth"),
    ("contract_invariants.session_engine_factory", "."),
    ("contract_invariants.validation_theatre", "src/elspeth"),
    ("immutability.freeze_guards", "src/elspeth"),
    ("immutability.frozen_annotations", "src/elspeth"),
    ("manifest.contract_manifest", "src/elspeth"),
    ("manifest.symbol_inventory", "."),
    ("manifest.test_to_source_mapping", "."),
    ("meta.no-new-bespoke-cicd-enforcer", "."),
    ("plugin_contract.component_type", "src/elspeth"),
    ("plugin_contract.options_metadata", "."),
    ("plugin_contract.plugin_hashes", "src/elspeth"),
    ("trust_tier.tier_model", "src/elspeth"),
)


class BaselineGenerationError(ValueError):
    """Verification, capture or publication refused without blessing a baseline."""


class BaselinePublicationUncertainError(OSError):
    """Replacement happened, but its durability could not be confirmed."""


@dataclass(frozen=True, slots=True)
class FrozenInputs:
    head: str
    index_sha256: str
    missing: tuple[str, ...]
    files: tuple[dict[str, str], ...]
    baseline_mode: int


def require_publication_custody(root: Path) -> None:
    """Require externally provisioned custody, never chmod a candidate checkout.

    A digest recheck cannot prevent an untrusted writer racing the subsequent
    rename. Every input and its path ancestry must already be owned exclusively
    by root/the admitted operator, including ordinary/linked Git metadata.
    """
    evidence = operator_runtime.require_operator_runtime()
    owners = {0, evidence.operator_uid}

    def protected(path: Path, *, link: bool = False) -> None:
        for current in (*reversed(path.parents), path):
            metadata = current.lstat()
            if metadata.st_uid not in owners or (not stat.S_ISLNK(metadata.st_mode) and metadata.st_mode & 0o022):
                raise BaselineGenerationError("baseline publication requires externally protected checkout custody")
            if current != path and not stat.S_ISDIR(metadata.st_mode):
                raise BaselineGenerationError("publication input ancestry is not protected")
            if current == path and stat.S_ISLNK(metadata.st_mode) and not link:
                raise BaselineGenerationError("publication metadata symlink is unsupported")

    protected(root)
    for relative in _census(root, ignored=frozenset(), include_directories=True):
        protected(root / relative, link=True)
    with TrustedGitRepository(root) as repository:
        marker = root / ".git"
        protected(marker)
        git_root = marker
        if marker.is_file():
            raw, _mode = _read_file(marker, limit=4096)
            git_root = Path(os.path.abspath(root / raw[8:].strip().decode("utf-8")))
        pending = [git_root, repository._common_dir]
        visited: set[Path] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            if len(visited) > 100000:
                raise BaselineGenerationError("protected Git metadata census exceeds its bound")
            protected(current)
            if current.is_dir():
                pending.extend(current.iterdir())


@contextmanager
def publication_lock(root: Path) -> Iterator[None]:
    name = "baseline-" + hashlib.sha256(os.fsencode(root)).hexdigest() + ".lock"
    path = operator_runtime.runtime_private_directory() / name
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise BaselineGenerationError("protected baseline operation lock is unavailable")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BaselineGenerationError("another baseline operation holds publication custody") from exc
        yield
    finally:
        os.close(descriptor)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _census(root: Path, *, ignored: frozenset[str], include_directories: bool = False) -> set[str]:
    """Enumerate non-cache inputs, including empty directories for custody."""
    paths: set[str] = set()
    pending = [Path()]
    visited = 0
    files = 0
    while pending:
        relative = pending.pop()
        descriptor = _open_directory(root / relative)
        try:
            with os.scandir(descriptor) as children:
                for child in children:
                    visited += 1
                    if visited > _MAX_FILES * 4:
                        raise BaselineGenerationError("repository input census exceeds its bound")
                    path = relative / child.name
                    parts = path.parts
                    if child.name in EXCLUDED_WALK_DIRS or any(parts[: len(prefix)] == prefix for prefix in EXCLUDED_WALK_PREFIXES):
                        continue
                    if path.as_posix() in ignored:
                        continue
                    if child.is_dir(follow_symlinks=False):
                        pending.append(path)
                        if include_directories:
                            paths.add(path.as_posix())
                    else:
                        paths.add(path.as_posix())
                        files += 1
                        if files > _MAX_FILES:
                            raise BaselineGenerationError("repository input census exceeds its bound")
        finally:
            os.close(descriptor)
    return paths


def freeze_inputs(root: Path, *, ignored: frozenset[str] = frozenset()) -> FrozenInputs:
    """Bind full tracked bytes/modes, missing paths, index and HEAD, not just Python."""
    try:
        with TrustedGitRepository(root) as repository:
            if repository.repo_root != root:
                raise BaselineGenerationError("baseline input must be the repository root")
            index = repository.index()
            if len(index) > _MAX_FILES:
                raise BaselineGenerationError("repository tracked census exceeds its bound")
            discovered = _census(root, ignored=ignored)
            if discovered - index.keys():
                raise BaselineGenerationError("untracked repository inputs must be staged or removed before generation")
            records: list[dict[str, str]] = []
            missing: list[str] = []
            total = 0
            for relative in sorted(index):
                if index[relative].mode == "160000":
                    raise BaselineGenerationError("baseline capture does not support Git submodules")
                try:
                    entry = _working_entry(root / relative)
                except FileNotFoundError:
                    missing.append(relative)
                    continue
                if entry is None:
                    raise BaselineGenerationError("a tracked file was replaced by a directory")
                raw, mode = entry
                key = os.environ.get("ELSPETH_JUDGE_METADATA_HMAC_KEY")
                if key and key.encode("utf-8") in raw:
                    raise BaselineGenerationError("repository input contains the selected signing value")
                total += len(raw)
                if len(raw) > _MAX_FILE_BYTES or total > _MAX_TOTAL_BYTES:
                    raise BaselineGenerationError("repository inputs exceed capture byte bounds")
                records.append(
                    {
                        "path": relative,
                        "mode": mode,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "content": base64.b64encode(raw).decode("ascii"),
                    }
                )
            indexed = [(name, entry.mode, entry.oid) for name, entry in sorted(index.items())]
            _baseline, baseline_mode = _read_file(root / BASELINE_PATH, limit=_MAX_FILE_BYTES)
            permissions = stat.S_IMODE(baseline_mode)
            if permissions & ~0o777:
                raise BaselineGenerationError("baseline special permission bits are unsupported")
            return FrozenInputs(
                repository.head(), hashlib.sha256(_canonical(indexed)).hexdigest(), tuple(missing), tuple(records), permissions
            )
    except (OSError, SourceSnapshotError) as exc:
        raise BaselineGenerationError("cannot freeze baseline repository inputs safely") from exc


def _materialize(frozen: FrozenInputs, target: Path) -> None:
    # Validation is shared with the external supervisor. It permits only symlinks
    # whose filesystem resolution stays inside the snapshot, and verifies every
    # declared digest and resource bound.
    from elspeth_lints.core.baseline_rpc import validate_files

    records = validate_files(list(frozen.files))
    for item in records:
        path = target / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if item["mode"] == "120000":
            path.symlink_to(base64.b64decode(item["content"]).decode("utf-8"))
        else:
            path.write_bytes(base64.b64decode(item["content"]))
            path.chmod(0o500 if item["mode"] == "100755" else 0o400)


def runtime_packages() -> tuple[tuple[Path, str, bool], ...]:
    import elspeth
    import elspeth_lints

    if elspeth.__file__ is None or elspeth_lints.__file__ is None:
        raise BaselineGenerationError("authenticated first-party packages must have concrete source roots")
    return (
        (Path(elspeth.__file__).parent, "src/elspeth", True),
        (Path(elspeth_lints.__file__).parent, "elspeth-lints/src/elspeth_lints", False),
    )


def require_runtime_inputs(frozen: FrozenInputs) -> None:
    """Bind runtime-backed catalog/rule execution to the same first-party bytes.

    Authenticated installed code is trusted to execute, but an older installed
    catalog must not silently stand in for the frozen candidate catalog.
    """
    for installed, prefix, python_only in runtime_packages():
        records = {row["path"][len(prefix) + 1 :]: row for row in frozen.files if row["path"].startswith(prefix + "/")}
        if python_only:
            records = {name: row for name, row in records.items() if name.endswith(".py")}
        if not records or "__init__.py" not in records:
            raise BaselineGenerationError("frozen first-party package census is incomplete")
        actual: dict[str, str] = {}
        pending = [installed]
        while pending:
            directory = pending.pop()
            for path in directory.iterdir():
                if path.name == "__pycache__":
                    continue
                if path.is_symlink():
                    raise BaselineGenerationError("installed first-party package contains a symlink")
                if path.is_dir():
                    pending.append(path)
                elif not python_only or path.suffix == ".py":
                    raw, _mode = _read_file(path, limit=_MAX_FILE_BYTES)
                    actual[path.relative_to(installed).as_posix()] = hashlib.sha256(raw).hexdigest()
                    if len(actual) > _MAX_FILES:
                        raise BaselineGenerationError("installed first-party census exceeds its bound")
        if actual != {name: row["sha256"] for name, row in records.items()}:
            raise BaselineGenerationError("authenticated first-party runtime differs from the frozen candidate; rebuild and qualify it")


@contextmanager
def _scan_context(root: Path) -> Iterator[None]:
    # Some installed rules discover their config from cwd/ancestors. Bind that
    # discovery to the private snapshot, never the live checkout or caller cwd.
    with _SCAN_LOCK:
        previous = Path.cwd()
        prior_mode = os.environ.get(_VERIFY_MODE)
        os.environ[_VERIFY_MODE] = "required"
        os.chdir(root)
        try:
            yield
        finally:
            os.chdir(previous)
            if prior_mode is None:
                os.environ.pop(_VERIFY_MODE, None)
            else:
                os.environ[_VERIFY_MODE] = prior_mode


def _scan(root: Path, rule: str, relative: str, *, raw: bool = False) -> tuple[int, list[dict[str, Any]]]:
    from elspeth_lints.core import cli

    arguments = ["check", "--rules", rule, "--root", str(root / relative), "--repo-root", str(root), "--format", "json"]
    if raw:
        empty = root.parent / "empty-allowlist"
        empty.mkdir(exist_ok=True)
        arguments.extend(("--allowlist-dir", str(empty)))
    elif rule == "trust_tier.tier_model":
        arguments.extend(("--allowlist-dir", str(root / "config/cicd/enforce_tier_model")))
    output = io.StringIO()
    try:
        with _scan_context(root), redirect_stdout(output), redirect_stderr(io.StringIO()):
            result = cli.main(arguments)
    except (OSError, ValueError) as exc:
        raise BaselineGenerationError("installed rule refused the frozen inputs") from exc
    try:
        parsed = strict_json_loads(output.getvalue())
    except (StrictJSONError, RecursionError) as exc:
        raise BaselineGenerationError("installed rule did not return an unambiguous finding census") from exc
    if type(parsed) is not list or any(type(row) is not dict for row in parsed):
        raise BaselineGenerationError("installed rule returned an invalid finding census")
    if any(row.get("severity") not in {"note", "warning", "error"} for row in parsed):
        raise BaselineGenerationError("installed rule returned an invalid severity")
    has_gating_findings = any(row["severity"] != "note" for row in parsed)
    if result not in {0, 1} or (result != 0) != has_gating_findings:
        raise BaselineGenerationError("installed rule finding census disagrees with its result")
    return result, parsed


def verify_signed_gate(root: Path) -> None:
    operator_runtime.require_operator_runtime()
    if not os.environ.get("ELSPETH_JUDGE_METADATA_HMAC_KEY"):
        raise BaselineGenerationError("required signed verification needs the operator-held key")
    if not any(iter_python_files(root / "src/elspeth")):
        raise BaselineGenerationError("signed enforce gate has no source inputs")
    if not (root / "config/cicd/enforce_tier_model/_defaults.yaml").is_file():
        raise BaselineGenerationError("signed enforce allowlist is missing")
    result, findings = _scan(root, "trust_tier.tier_model", "src/elspeth")
    if result != 0 or any(row["severity"] != "note" for row in findings):
        raise BaselineGenerationError("required signed enforce gate refused; no baseline was published")


def capture_fingerprints(root: Path) -> dict[str, list[dict[str, str]]]:
    """Rederive the canonical raw census using authenticated installed rule code."""
    captured: dict[str, list[dict[str, str]]] = {}
    for rule, relative in RULE_ROOTS:
        _result, findings = _scan(root, rule, relative, raw=rule == "trust_tier.tier_model")
        rows: list[dict[str, str]] = []
        for finding in findings:
            if type(finding.get("file_path")) is not str or type(finding.get("fingerprint")) is not str:
                raise BaselineGenerationError("installed finding lacks its canonical path or fingerprint")
            path = finding["file_path"]
            if Path(path).is_absolute():
                try:
                    path = Path(path).relative_to(root / relative).as_posix()
                except ValueError as exc:
                    # Runtime-backed metadata reports installed source paths.
                    # These bytes were independently matched to this snapshot.
                    matched = [
                        (Path(prefix) / Path(path).relative_to(installed)).relative_to(relative).as_posix()
                        for installed, prefix, _python in runtime_packages()
                        if Path(path).is_relative_to(installed)
                        and (Path(prefix) / Path(path).relative_to(installed)).is_relative_to(relative)
                    ]
                    if len(matched) != 1:
                        raise BaselineGenerationError("installed finding escaped its selected root") from exc
                    path = matched[0]
            rows.append({"file_path": path, "fingerprint": finding["fingerprint"]})
        captured[rule] = sorted(rows, key=lambda row: (row["file_path"], row["fingerprint"]))
    return captured


def run_capture(*, files: list[dict[str, str]], snapshot_sha256: str, expected_baseline: dict[str, list[dict[str, str]]]) -> None:
    from elspeth_lints.core import baseline_rpc

    baseline_rpc.run_capture(files=files, snapshot_sha256=snapshot_sha256, expected_baseline=expected_baseline)


def _unchanged(root: Path, frozen: FrozenInputs, *, ignored: frozenset[str] = frozenset()) -> None:
    if freeze_inputs(root, ignored=ignored) != frozen:
        raise BaselineGenerationError("repository inputs changed before baseline publication")


def _publish(root: Path, frozen: FrozenInputs, payload: bytes) -> None:
    target = root / BASELINE_PATH
    parent = _open_directory(target.parent)
    temporary = f".fingerprint-baseline-{uuid.uuid4().hex}.tmp"
    created = False
    try:
        existing = next(item for item in frozen.files if item["path"] == BASELINE_PATH.as_posix())
        if existing["mode"] not in {"100644", "100755"}:
            raise BaselineGenerationError("baseline must be an existing regular tracked file")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        created = True
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fchmod(output.fileno(), frozen.baseline_mode)
            os.fsync(output.fileno())
        ignored = frozenset({(BASELINE_PATH.parent / temporary).as_posix()})
        _unchanged(root, frozen, ignored=ignored)
        current = _open_directory(target.parent)
        try:
            if (os.fstat(parent).st_dev, os.fstat(parent).st_ino) != (os.fstat(current).st_dev, os.fstat(current).st_ino):
                raise BaselineGenerationError("baseline parent changed before publication")
        finally:
            os.close(current)
        os.fsync(parent)
        os.replace(temporary, target.name, src_dir_fd=parent, dst_dir_fd=parent)
        created = False
        try:
            os.fsync(parent)
        except OSError as exc:
            raise BaselinePublicationUncertainError("baseline replacement durability is unknown") from exc
    finally:
        if created:
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)


def regenerate(root: Path) -> None:
    evidence = operator_runtime.require_operator_runtime()
    if evidence.python_version != (3, 13):
        raise BaselineGenerationError("fingerprint baseline generation requires the protected Python 3.13 build")
    root = Path(os.path.abspath(root))
    require_publication_custody(root)
    with publication_lock(root):
        _regenerate(root)


def _regenerate(root: Path) -> None:
    frozen = freeze_inputs(root)
    require_runtime_inputs(frozen)
    if not any(item["path"] == BASELINE_PATH.as_posix() for item in frozen.files):
        raise BaselineGenerationError("canonical tracked baseline is missing")
    with tempfile.TemporaryDirectory(prefix="baseline-verification-", dir=operator_runtime.runtime_private_directory()) as temporary:
        private = Path(temporary) / "repository"
        private.mkdir()
        _materialize(frozen, private)
        verify_signed_gate(private)
        expected = capture_fingerprints(private)
        files = list(frozen.files)
        run_capture(files=files, snapshot_sha256=hashlib.sha256(_canonical(files)).hexdigest(), expected_baseline=expected)
        # Candidate callbacks never see this private verification tree. Still
        # rederive clearance before checking every live input and publication.
        verify_signed_gate(private)
        if capture_fingerprints(private) != expected:
            raise BaselineGenerationError("trusted capture changed before baseline publication")
        require_publication_custody(root)
        _unchanged(root, frozen)
        _publish(root, frozen, (json.dumps(expected, indent=2) + "\n").encode("utf-8"))
