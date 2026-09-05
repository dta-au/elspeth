"""Every ``ELSPETH_WEB__*`` name the deploy tree or a runbook exports is a real setting.

``settings_from_env`` refuses an unknown ``ELSPETH_WEB__`` name, so a task
definition, a Compose file or a runbook that still exports a setting after it
has been deleted from ``WebSettings`` does not degrade -- the service refuses
to boot, and the runbook walks the operator into that refusal. The deletion of
``oidc_authorization_allowed_origins`` (identity sprint step C) left exactly
that in ``deploy/aws-ecs/terraform/modules/scenario/locals.tf`` and the ECS
runbook, and the runbook's own contract test could not see it: it pins that
names are PRESENT in the text, not that they RESOLVE.

This is the pin that closes the class. It reads the same trees an operator
does and resolves each name against the live field set; there is no allowlist
of known-stale names, because a known-stale export is the defect.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from elspeth.web.config import WebSettings

REPO_ROOT = Path(__file__).resolve().parents[3]
_EXPORT_TREES = (REPO_ROOT / "deploy", REPO_ROOT / "docs" / "runbooks")
_TEXT_SUFFIXES = frozenset(
    {".tf", ".tfvars", ".md", ".yaml", ".yml", ".json", ".sh", ".env", ".txt", ".py", ".toml", ".bicep", ".service", ".example"}
)
_ENV_NAME = re.compile(r"ELSPETH_WEB__([A-Z0-9_]+)")
# Reserved by settings_from_env: it is refused by name so a task definition
# cannot pin a region the ambient AWS_REGION disagrees with. A runbook may
# name it only to say so.
_RESERVED = frozenset({"DEPLOYMENT_AWS_REGION"})


def _export_files() -> list[Path]:
    files: list[Path] = []
    for tree in _EXPORT_TREES:
        assert tree.is_dir(), f"expected {tree} to exist"
        files.extend(
            path for path in sorted(tree.rglob("*")) if path.is_file() and path.suffix in _TEXT_SUFFIXES and "__pycache__" not in path.parts
        )
    return files


def _exported_names() -> dict[str, list[str]]:
    names: dict[str, list[str]] = {}
    for path in _export_files():
        text = path.read_text(encoding="utf-8", errors="strict")
        for match in _ENV_NAME.finditer(text):
            names.setdefault(match.group(1), []).append(str(path.relative_to(REPO_ROOT)))
    return names


def test_the_export_trees_are_actually_scanned() -> None:
    names = _exported_names()
    assert len(names) >= 40, f"expected the deploy tree and runbooks to name dozens of settings, found {len(names)}"
    assert "AUTH_PROVIDER" in names and "SESSION_DB_URL" in names


def test_every_exported_web_setting_resolves_to_a_live_field() -> None:
    fields = set(WebSettings.model_fields)
    unresolved = {
        name: sorted(set(paths)) for name, paths in _exported_names().items() if name.lower() not in fields and name not in _RESERVED
    }
    assert unresolved == {}, (
        "ELSPETH_WEB__ names exported by the deploy tree or a runbook that are not WebSettings fields "
        "(the service refuses to boot on an unknown setting): " + repr(unresolved)
    )


@pytest.mark.parametrize("deleted", ["OIDC_AUTHORIZATION_ALLOWED_ORIGINS", "RELEASE_SOURCE_SHA"])
def test_the_two_stale_exports_this_pin_was_written_for_stay_gone(deleted: str) -> None:
    assert deleted not in _exported_names()
