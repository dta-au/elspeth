"""Contracts for dependencies required by every ELSPETH installation."""

from __future__ import annotations

import tomllib
from pathlib import Path

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
