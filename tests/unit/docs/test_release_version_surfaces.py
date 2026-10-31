"""Release-version consistency across package and current public surfaces."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

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


def _scenario_a_compatibility_record() -> dict[str, object]:
    runbook = _text("docs/runbooks/aws-ecs-deployment.md")
    heading = runbook.index("### Bound release/schema compatibility record")
    fence = runbook.index("```json\n", heading) + len("```json\n")
    record, _ = json.JSONDecoder().raw_decode(runbook[fence:])
    assert isinstance(record, dict)
    return record


def _new_store_jq_filter() -> str:
    runbook = _text("docs/runbooks/aws-ecs-deployment.md")
    start = runbook.index("require_compatibility_record_current() {")
    end = runbook.index("\n}\n", start)
    matches = re.findall(
        r"^  jq -e '\n(?P<query>(?:    [^\n]*\n)+)  ' \"\$receipt\" >/dev/null$",
        runbook[start:end],
        re.MULTILINE,
    )
    assert len(matches) == 1
    return matches[0]


def _rollback_refusal_jq_filter() -> str:
    """Read the retained diagnostic predicate, which grants no deployment authority."""
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
    # Sentence case, not Title Case: the README's heading style. Pinned exactly
    # rather than case-insensitively — tolerating both spellings would let the
    # heading drift back.
    assert _match_version("README.md", rf"^## What changed in (?P<version>{SEMVER})$") == current_version


def test_current_release_surface_validator_rejects_unreleased_heading(monkeypatch: pytest.MonkeyPatch) -> None:
    test_current_public_release_surfaces_match_package_version()
    original_text = _text
    current_version = _project_version()
    changelog = original_text("CHANGELOG.md")
    current_heading = next(line for line in changelog.splitlines() if line.startswith(f"## {current_version} - "))
    drifted = changelog.replace(current_heading, f"## Unreleased ({current_version})", 1)
    assert drifted != changelog

    def text_with_unreleased_heading(relative_path: str) -> str:
        return drifted if relative_path == "CHANGELOG.md" else original_text(relative_path)

    monkeypatch.setattr(f"{__name__}._text", text_with_unreleased_heading)
    with pytest.raises(AssertionError):
        test_current_public_release_surfaces_match_package_version()


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


def test_scenario_a_runbook_record_matches_live_new_store_derivation() -> None:
    record = _scenario_a_compatibility_record()

    assert record["schema"] == "elspeth.aws-ecs-compatibility-record.v3"
    assert record["scenario_id"] == "A"
    assert record["candidate_package_version"] == receipt_contracts._CANDIDATE_PACKAGE_VERSION == _project_version()
    assert record["schema_facts"] == receipt_contracts._expected_schema_facts("A")
    assert record["new_store_compatible"] is True
    assert record["backward_compatible"] is False
    assert record["rollback_permitted"] is False
    assert "forward_compatible" not in record
    for field in (
        "previous_source_sha",
        "previous_image_digest",
        "previous_task_definition",
        "rollback_doctor_task_definition",
        "previous_package_version",
    ):
        assert record[field] == "", field


def test_current_executable_jq_gate_requires_new_store_claim_and_refuses_downgrade() -> None:
    payload = _scenario_a_compatibility_record()
    query = _new_store_jq_filter()

    accepted = _run_jq(query, payload)
    assert accepted.returncode == 0, accepted.stderr

    for field in ("new_store_compatible", "backward_compatible", "rollback_permitted"):
        drifted = json.loads(json.dumps(payload))
        drifted[field] = not payload[field]
        rejected = _run_jq(query, drifted)
        assert rejected.returncode == 1, (field, rejected.stdout, rejected.stderr)

    legacy = json.loads(json.dumps(payload))
    legacy["forward_compatible"] = legacy.pop("new_store_compatible")
    assert _run_jq(query, legacy).returncode == 1


def test_scenario_b_stops_before_documented_provisioning_with_negative_control() -> None:
    runbook = _text("docs/runbooks/aws-ecs-deployment.md")
    for name, arguments in (
        ("activate_scenario_stack", "B"),
        ("plan_and_apply_scenario", "B unused unused unused unused unused"),
        ("provision_scenario_storage", ""),
    ):
        start = runbook.index(f"{name}() {{")
        end = runbook.index("\n}\n", start) + len("\n}")
        function = runbook[start:end]
        guard_start = function.index("  if test ")
        guard_end = function.index("  fi\n", guard_start) + len("  fi\n")
        # A shell call beyond the policy stop is the observable failure. Removing
        # the stop must make this controlled copy reach that call.
        controlled = function[:guard_end] + "  printf 'provisioning_reached\\n'\n}"
        prefix = "ACTIVE_SCENARIO_ID=B\n" + controlled + f"\n{name} {arguments}\n"
        stopped = subprocess.run(["bash", "-c", prefix], capture_output=True, text=True, check=False)
        assert stopped.returncode == 64, (name, stopped.stderr)
        assert stopped.stderr == "compatibility_transition_unsupported\n", name
        assert stopped.stdout == "", name
        without_stop = controlled[:guard_start] + controlled[guard_end:]
        negative = subprocess.run(
            ["bash", "-c", "ACTIVE_SCENARIO_ID=B\n" + without_stop + f"\n{name} {arguments}\n"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert negative.returncode == 0, (name, negative.stderr)
        assert negative.stdout == "provisioning_reached\n", name


def test_historical_scenario_b_baseline_stops_before_storage_and_schema_work() -> None:
    runbook = _text("docs/runbooks/aws-ecs-deployment.md")
    start = runbook.index("### Fresh Scenario B upgrade baseline")
    fence = runbook.index("```bash\n", start) + len("```bash\n")
    end = runbook.index("```", fence)
    script = runbook[fence:end]
    assert script.index("exit 64") < script.index("load_scenario B") < script.index("provision_scenario_storage")
    stopped = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert stopped.returncode == 64
    assert stopped.stderr == "compatibility_transition_unsupported\n"
    assert stopped.stdout == ""
