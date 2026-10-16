"""Contracts for dependencies required by every ELSPETH installation."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import tomllib
from pathlib import Path

import pytest
import typer
from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_typer_runtime_matches_declared_distribution() -> None:
    """The CLI must execute the supported Typer, regardless of install order."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    declared = {Requirement(item).name: Requirement(item) for item in project["dependencies"]}
    assert typer.__version__ == importlib.metadata.version("typer")
    assert typer.__version__ in declared["typer"].specifier


def test_typer_import_files_have_one_owner() -> None:
    """A transitive CLI distribution must not overwrite the full Typer package."""
    owner = importlib.metadata.distribution("typer")
    owned = [path for path in owner.files or () if str(path).startswith("typer/") and str(path).endswith(".py")]
    assert owned
    for distribution in importlib.metadata.distributions():
        if distribution.metadata["Name"] == "typer":
            continue
        overlap = set(owned).intersection(distribution.files or ())
        assert not overlap, f"{distribution.metadata['Name']} also owns {sorted(map(str, overlap))}"


def test_typer_import_files_match_distribution_record() -> None:
    """Metadata alone cannot certify package files written by another wheel."""
    owner = importlib.metadata.distribution("typer")
    assert Path(typer.__file__).resolve() == owner.locate_file("typer/__init__.py").resolve()
    owned = [path for path in owner.files or () if str(path).startswith("typer/") and str(path).endswith(".py")]
    assert owned
    for path in owned:
        assert path.hash is not None and path.hash.mode == "sha256"
        digest = base64.urlsafe_b64encode(hashlib.sha256(owner.locate_file(path).read_bytes()).digest()).decode().rstrip("=")
        assert digest == path.hash.value, f"Installed {path} differs from Typer RECORD"


def test_rfc8785_is_a_base_runtime_dependency() -> None:
    """Canonical audit JSON must work without selecting any package extra."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    base_names = {Requirement(item).name for item in project["dependencies"]}
    optional_names = {Requirement(item).name for dependencies in project["optional-dependencies"].values() for item in dependencies}

    assert "rfc8785" in base_names
    assert "rfc8785" not in optional_names


def test_base_fsspec_declaration_and_lock_exclude_unsafe_reference_templates() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    declared = {Requirement(item).name: Requirement(item) for item in project["dependencies"]}
    requirement = declared["fsspec"]
    assert "2026.1.0" not in requirement.specifier
    assert "2026.6.0" in requirement.specifier
    locked = tomllib.loads((REPO_ROOT / "uv.lock").read_text())["package"]
    selected = [package for package in locked if package["name"] == "fsspec"]
    assert len(selected) == 1
    assert selected[0]["version"] in requirement.specifier


@pytest.mark.parametrize("extra", ["llm", "webui", "all"])
def test_each_litellm_extra_excludes_leaking_multidict_versions(extra: str) -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    declared = {Requirement(item).name: Requirement(item) for item in project["optional-dependencies"][extra]}
    assert "litellm" in declared
    requirement = declared["multidict"]
    assert "6.7.0" not in requirement.specifier
    assert "6.9.0" not in requirement.specifier
    assert "6.9.1" in requirement.specifier
    base_names = {Requirement(item).name for item in project["dependencies"]}
    assert "multidict" not in base_names
    locked = tomllib.loads((REPO_ROOT / "uv.lock").read_text())["package"]
    selected = [package for package in locked if package["name"] == "multidict"]
    assert len(selected) == 1
    assert selected[0]["version"] in requirement.specifier
