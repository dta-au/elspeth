"""Release-version consistency across package and current public surfaces."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web._aws_ecs_acceptance import receipt_contracts
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

ROOT = Path(__file__).resolve().parents[3]
SEMVER = r"\d+\.\d+\.\d+"
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _project_version() -> str:
    return tomllib.loads(_text("pyproject.toml"))["project"]["version"]


def _match_version(relative_path: str, pattern: str) -> str:
    match = re.search(pattern, _text(relative_path), re.MULTILINE)
    assert match is not None, relative_path
    return match.group("version")


def _scenario_b_compatibility_record() -> dict[str, object]:
    runbook = _text("docs/runbooks/aws-ecs-deployment.md")
    heading = runbook.index("### Bound release/schema compatibility record")
    fence = runbook.index("```json\n", heading) + len("```json\n")
    record, _ = json.JSONDecoder().raw_decode(runbook[fence:])
    assert isinstance(record, dict)
    return record


def _rollback_refusal_jq_filter() -> str:
    runbook = _text("docs/runbooks/aws-ecs-deployment.md")
    matches = re.findall(
        r"^  jq -e '\n(?P<query>(?:    [^\n]*\n)+)  ' \"\$ROLLBACK_REFUSAL_RECEIPT\" >/dev/null$",
        runbook,
        re.MULTILINE,
    )
    assert len(matches) == 1
    return matches[0]


def _run_jq(query: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["jq", "-e", query],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )


def test_package_and_lockfile_use_current_release_version() -> None:
    pyproject = tomllib.loads(_text("pyproject.toml"))
    lockfile = tomllib.loads(_text("uv.lock"))
    locked_project = next(package for package in lockfile["package"] if package["name"] == "elspeth")

    assert locked_project["version"] == pyproject["project"]["version"]


def test_current_public_release_surfaces_match_package_version() -> None:
    current_version = _project_version()
    assert _match_version("docs/README.md", rf"^\*\*Framework status:\*\* `(?P<version>{SEMVER})`$") == current_version
    assert _match_version("CHANGELOG.md", rf"^## (?P<version>{SEMVER})\s+-\s+.+$") == current_version
    assert _match_version("README.md", rf"^!\[Status: (?P<version>{SEMVER})\]\([^)]+\)$") == current_version
    assert _match_version("README.md", rf"^## What Changed In (?P<version>{SEMVER})$") == current_version


def test_release_markdown_links_resolve() -> None:
    broken: list[str] = []
    for markdown in sorted((ROOT / "docs" / "release").glob("*.md")):
        for destination in MARKDOWN_LINK_RE.findall(markdown.read_text(encoding="utf-8")):
            relative_target = destination.split("#", maxsplit=1)[0]
            if not relative_target or "://" in relative_target or relative_target.startswith("mailto:"):
                continue
            if not (markdown.parent / relative_target).exists():
                broken.append(f"{markdown.relative_to(ROOT)} -> {destination}")
    assert broken == []


def test_current_container_examples_require_a_confirmed_published_tag() -> None:
    current_version = _project_version()
    for relative_path in (
        "README.md",
        "docs/guides/docker.md",
        "docs/guides/troubleshooting.md",
        "docs/guides/your-first-pipeline.md",
        "docs/reference/environment-variables.md",
        "docs/runbooks/resume-failed-run.md",
    ):
        text = _text(relative_path)
        assert "IMAGE_TAG" in text, relative_path
        assert f"v{current_version}" not in text, relative_path
        assert "v0.7.1" not in text, relative_path
        assert "elspeth:latest" not in text, relative_path

    docker = _text("docs/guides/docker.md")
    assert "docker buildx imagetools inspect" in docker


def test_first_pipeline_docker_walkthrough_creates_the_mounted_state_directory() -> None:
    tutorial = _text("docs/guides/your-first-pipeline.md")

    assert "mkdir -p my-pipeline/{config,input,output,data}" in tutorial
    assert "-v $(pwd)/data:/app/data" in tutorial
    assert "mkdir -p my-pipeline/{config,input,output,state}" not in tutorial


def test_operator_schema_version_examples_match_live_constants() -> None:
    sharing = _text("docs/guides/sharing-pipelines.md")
    assert f"SESSION_SCHEMA_EPOCH={SESSION_SCHEMA_EPOCH}" in sharing
    assert f"SQLITE_SCHEMA_EPOCH={SQLITE_SCHEMA_EPOCH}" in sharing


def test_scenario_b_runbook_record_matches_live_release_derivation() -> None:
    record = _scenario_b_compatibility_record()

    assert record["candidate_package_version"] == receipt_contracts._CANDIDATE_PACKAGE_VERSION == _project_version()
    assert record["previous_package_version"] == receipt_contracts._ROLLBACK_PACKAGE_VERSION
    assert record["schema_facts"] == receipt_contracts._expected_schema_facts("B")


def test_scenario_b_executable_jq_gate_binds_live_landscape_epochs() -> None:
    expected_facts = receipt_contracts._expected_schema_facts("B")
    payload: dict[str, object] = {
        "backward_compatible": False,
        "rollback_permitted": False,
        "schema_facts": expected_facts,
    }
    query = _rollback_refusal_jq_filter()

    accepted = _run_jq(query, payload)
    assert accepted.returncode == 0, accepted.stderr

    for side in ("candidate", "previous"):
        drifted = json.loads(json.dumps(payload))
        drifted["schema_facts"][side]["landscape_epoch"] += 1
        rejected = _run_jq(query, drifted)
        assert rejected.returncode == 1, (side, rejected.stdout, rejected.stderr)
