"""Every ``ELSPETH_WEB__*`` name the deploy tree or a runbook exports is a real setting.

``settings_from_env`` refuses an unknown ``ELSPETH_WEB__`` name, so a task
definition, a Compose file or a runbook that still exports a setting after it
has been deleted from ``WebSettings`` does not degrade -- the service refuses
to boot, and the runbook walks the operator into that refusal. The deletion of
``oidc_authorization_allowed_origins`` (identity sprint step C) left exactly
that in ``deploy/aws-ecs/terraform/modules/scenario/locals.tf`` and the ECS
runbook, and the runbook's own contract test could not see it: it pins that
names are PRESENT in the text, not that they RESOLVE.

This is the pin that closes the class. It reads the TRACKED deploy tree and
runbooks (``git ls-files``, never whatever happens to be on the box: a
gitignored ``deploy/**/*.env`` is the live service's environment, not the
release's) and resolves each name against the live field set; there is no
allowlist of known-stale names, because a known-stale export is the defect.

A name inside a comment is not an export. The first version of this test
matched ``ELSPETH_WEB__COMPOSER_ADVISOR_ENABLED`` inside the comment
``# (removed) ELSPETH_WEB__COMPOSER_ADVISOR_ENABLED -- ...`` of an untracked
env file and reported the release stale for a setting the comment said was
gone. Every other line counts: the ECS task definition names settings as
``{ name = "ELSPETH_WEB__HOST", ... }`` map entries and the runbooks name them
in prose and in ``export`` lines, and a runbook that names a deleted setting
is exactly the defect this pin exists to catch.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from elspeth.web.config import WebSettings

REPO_ROOT = Path(__file__).resolve().parents[3]
_EXPORT_TREES = ("deploy", "docs/runbooks")
_TEXT_SUFFIXES = frozenset(
    {".tf", ".tfvars", ".md", ".yaml", ".yml", ".json", ".sh", ".env", ".txt", ".py", ".toml", ".bicep", ".service", ".example"}
)
_ENV_NAME = re.compile(r"ELSPETH_WEB__([A-Z0-9_]+)")
# A line whose first non-blank token opens a comment in any of the formats the
# trees use: shell, YAML, Terraform, Dockerfile and env files (``#``);
# Terraform and JSON-with-comments (``//``).
_COMMENT_LINE = re.compile(r"^\s*(#|//)")
# Reserved by settings_from_env: it is refused by name so a task definition
# cannot pin a region the ambient AWS_REGION disagrees with. A runbook may
# name it only to say so.
_RESERVED = frozenset({"DEPLOYMENT_AWS_REGION"})


def _tracked_export_files(root: Path) -> list[Path]:
    """The text files git tracks under the export trees of ``root``.

    ``git ls-files`` lists the index only: an untracked or gitignored file
    beside them -- a local ``.env``, a scratch copy -- is not part of the
    release and is not scanned.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--", *_EXPORT_TREES],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    files = [root / entry for entry in listing.decode("utf-8").split("\0") if entry]
    return [path for path in files if path.suffix in _TEXT_SUFFIXES and path.is_file()]


def _exported_names_in(text: str) -> list[str]:
    """Every ``ELSPETH_WEB__`` name on a non-comment line of ``text``, in order."""
    names: list[str] = []
    for line in text.splitlines():
        if _COMMENT_LINE.match(line):
            continue
        names.extend(match.group(1) for match in _ENV_NAME.finditer(line))
    return names


def _exported_names(root: Path) -> dict[str, list[str]]:
    names: dict[str, list[str]] = {}
    for path in _tracked_export_files(root):
        text = path.read_text(encoding="utf-8", errors="strict")
        for name in _exported_names_in(text):
            names.setdefault(name, []).append(str(path.relative_to(root)))
    return names


def test_the_export_trees_are_actually_scanned() -> None:
    names = _exported_names(REPO_ROOT)
    assert len(names) >= 40, f"expected the deploy tree and runbooks to name dozens of settings, found {len(names)}"
    assert "AUTH_PROVIDER" in names and "SESSION_DB_URL" in names


def test_every_exported_web_setting_resolves_to_a_live_field() -> None:
    fields = set(WebSettings.model_fields)
    unresolved = {
        name: sorted(set(paths))
        for name, paths in _exported_names(REPO_ROOT).items()
        if name.lower() not in fields and name not in _RESERVED
    }
    assert unresolved == {}, (
        "ELSPETH_WEB__ names exported by the deploy tree or a runbook that are not WebSettings fields "
        "(the service refuses to boot on an unknown setting): " + repr(unresolved)
    )


@pytest.mark.parametrize("deleted", ["OIDC_AUTHORIZATION_ALLOWED_ORIGINS", "RELEASE_SOURCE_SHA"])
def test_the_two_stale_exports_this_pin_was_written_for_stay_gone(deleted: str) -> None:
    assert deleted not in _exported_names(REPO_ROOT)


def test_a_commented_out_name_is_not_an_export() -> None:
    text = (
        "# (removed) ELSPETH_WEB__COMMENTED_OUT -- the field no longer exists\n"
        "  // ELSPETH_WEB__ALSO_COMMENTED = 1\n"
        "ELSPETH_WEB__EXPORTED=1\n"
        "export ELSPETH_WEB__EXPORTED_TOO=1\n"
        '    { name = "ELSPETH_WEB__MAP_ENTRY", value = "x" },\n'
        "Set `ELSPETH_WEB__IN_PROSE` before starting the service.\n"
    )
    assert _exported_names_in(text) == ["EXPORTED", "EXPORTED_TOO", "MAP_ENTRY", "IN_PROSE"]


def test_an_untracked_or_ignored_env_file_is_not_scanned(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "deploy").mkdir(parents=True)
    (root / "docs" / "runbooks").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / ".gitignore").write_text("deploy/**/*.env\n", encoding="utf-8")
    (root / "deploy" / "tracked.tfvars").write_text('ELSPETH_WEB__AUTH_PROVIDER = "local"\n', encoding="utf-8")
    subprocess.run(["git", "add", "--", ".gitignore", "deploy/tracked.tfvars"], cwd=root, check=True, capture_output=True)
    # The live service's environment beside the release: gitignored by the
    # pattern above, so it is never part of the index.
    (root / "deploy" / "elspeth-web.env").write_text("ELSPETH_WEB__IGNORED_LOCAL=1\n", encoding="utf-8")
    # A scratch file that matches no ignore pattern but was never added.
    (root / "docs" / "runbooks" / "scratch.md").write_text("ELSPETH_WEB__UNTRACKED_SCRATCH=1\n", encoding="utf-8")

    names = _exported_names(root)

    assert names == {"AUTH_PROVIDER": ["deploy/tracked.tfvars"]}
