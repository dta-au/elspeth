"""Adapter discovery and code identity shared by runtime and image admission."""

import hashlib
import importlib.metadata
import inspect
from pathlib import Path, PurePosixPath

from elspeth_llm_gateway.core.config import ConfigError
from elspeth_llm_gateway.reference.adapter import ReferenceV1InvokeAdapter
from elspeth_llm_gateway.sdk.protocol import AdapterProtocol

_ADAPTER_ENTRY_POINT_GROUP = "elspeth_llm_gateway.adapters"
_REFERENCE_ADAPTER_NAME = "reference_v1_invoke"


def _matching_entry_point(adapter_name: str) -> importlib.metadata.EntryPoint:
    entry_points = importlib.metadata.entry_points(group=_ADAPTER_ENTRY_POINT_GROUP)
    matches = [entry_point for entry_point in entry_points if entry_point.name == adapter_name]
    if not matches:
        raise ConfigError([f"unknown_adapter:{adapter_name}"])
    if len(matches) != 1:
        raise ConfigError([f"ambiguous_adapter:{adapter_name}"])
    return matches[0]


def resolve_adapter(adapter_name: str) -> AdapterProtocol:
    """Resolve an installed adapter by name: the reference adapter, or an entry point."""
    if adapter_name == _REFERENCE_ADAPTER_NAME:
        return ReferenceV1InvokeAdapter()

    adapter_cls = _matching_entry_point(adapter_name).load()
    return adapter_cls()


def _hash_files(files: list[tuple[str, Path]]) -> str:
    if not files:
        raise RuntimeError("adapter package source is unavailable")

    digest = hashlib.sha256()
    for relative, path in sorted(files):
        encoded_relative = relative.encode()
        content = path.read_bytes()
        digest.update(len(encoded_relative).to_bytes(8, "big"))
        digest.update(encoded_relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _source_package_files(adapter: AdapterProtocol, *, require_package: bool) -> list[tuple[str, Path]]:
    source_file = inspect.getsourcefile(type(adapter))
    if source_file is None:
        raise RuntimeError("adapter package source is unavailable")
    source_path = Path(source_file)
    package_root = source_path.parent
    if source_path.is_symlink() or not source_path.is_file():
        raise RuntimeError("adapter package source is unavailable")

    if not (package_root / "__init__.py").is_file():
        if require_package:
            raise RuntimeError("adapter package source is unavailable")
        return [(source_path.name, source_path)]

    files: list[tuple[str, Path]] = []
    for path in package_root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("adapter package contains a symlink")
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_file():
            files.append((path.relative_to(package_root).as_posix(), path))
    return files


def _distribution_files(entry_point: importlib.metadata.EntryPoint) -> list[tuple[str, Path]]:
    distribution = entry_point.dist
    if distribution is None:
        raise RuntimeError("adapter distribution inventory is unavailable")
    listed_files = distribution.files
    if not listed_files:
        raise RuntimeError("adapter distribution inventory is unavailable")

    distribution_root = Path(str(distribution.locate_file(""))).resolve()
    files: list[tuple[str, Path]] = []
    for listed_file in listed_files:
        relative = PurePosixPath(str(listed_file))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("adapter distribution inventory escapes its root")
        if "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}:
            continue

        path = Path(str(distribution.locate_file(listed_file)))
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("adapter distribution file is unavailable")
        try:
            path.resolve().relative_to(distribution_root)
        except ValueError as exc:
            raise RuntimeError("adapter distribution inventory escapes its root") from exc
        files.append((relative.as_posix(), path))
    return files


def compute_adapter_fingerprint(
    adapter: AdapterProtocol,
    *,
    adapter_name: str | None = None,
    require_package: bool = False,
) -> str:
    """Hash the complete installed adapter distribution or a test source package."""
    if adapter_name == _REFERENCE_ADAPTER_NAME:
        files = _source_package_files(adapter, require_package=True)
    elif adapter_name is not None:
        files = _distribution_files(_matching_entry_point(adapter_name))
    else:
        files = _source_package_files(adapter, require_package=require_package)
    return _hash_files(files)
