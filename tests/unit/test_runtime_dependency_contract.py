"""Contracts for dependencies required by every ELSPETH installation."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parents[2]


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
