"""Policy contract for opt-in live-provider evidence collection."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = REPO_ROOT / "pyproject.toml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "state-engine-live-provider.yml"
SELECTORS = "docs/architecture/state_engine/proof-catalog/v3/evidence_selectors.json"
PROFILE = "postgresql-16-aws-single-leader-landscape"
TEST_CASE_ID = "source:aws_s3@default"
TEST_LANE_ID = "protected-live-source-aws_s3-default"
JOBS_TO_LANES = {
    "live_aws": "live-aws",
    "live_azure": "live-azure",
    "live_dataverse": "live-dataverse",
    "live_chroma": "live-chroma",
    "live_provider": "live-provider",
}
ACTION_PINS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "astral-sh/setup-uv": "c771a70e6277c0a99b617c7a806ffedaca235ff9",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
}
EXACT_ARTIFACT_FILES = {
    "manifest.json",
    "junit.xml",
    "profile.json",
    "nodes.txt",
    "scrub-report.json",
}


def _workflow() -> dict[str, Any]:
    raw = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _triggers(workflow: dict[str, Any]) -> object:
    # PyYAML 1.1 admits the unquoted key ``on`` as boolean true.
    return workflow.get("on", workflow.get(True))


def _run_blocks(job: dict[str, Any]) -> str:
    return "\n".join(str(step["run"]) for step in job["steps"] if "run" in step)


def _run_synthetic_pytest(
    tmp_path: Path,
    source: str,
    *,
    marker_expression: str | None = None,
    run_live_provider: bool = False,
) -> subprocess.CompletedProcess[str]:
    nested = tmp_path / "tests" / "integration" / "plugins" / "sources"
    nested.mkdir(parents=True)
    test_file = nested / "test_synthetic_remote_live.py"
    test_file.write_text(source, encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWS_REGION=ap-southeast-2\n"
        "ELSPETH_TEST_S3_BUCKET=plausible-disposable-bucket\n"
        "ELSPETH_STATE_ENGINE_AWS_POSTGRES_URL=postgresql://plausible.invalid/state\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "tests.conftest",
        "-c",
        str(PYPROJECT),
        "--envfile",
        str(env_file),
        "-n",
        "0",
        "-q",
        str(test_file),
    ]
    if marker_expression is not None:
        command.extend(["-m", marker_expression])
    if run_live_provider:
        command.append("--run-live-provider")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT / "src"), str(REPO_ROOT)))
    return subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_live_provider_marker_is_strict_and_default_deselected() -> None:
    pytest_config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]

    assert any(str(marker).startswith("live_provider:") for marker in pytest_config["markers"])
    marker_expression = pytest_config["addopts"][pytest_config["addopts"].index("-m") + 1]
    assert "not live_provider" in marker_expression


def test_default_deselection_ignores_plausible_dotenv_credentials(tmp_path: Path) -> None:
    result = _run_synthetic_pytest(
        tmp_path,
        "import pytest\npytestmark = pytest.mark.live_provider\n"
        "def test_must_not_execute():\n    pytest.fail('live node executed without CLI authority')\n",
    )

    assert result.returncode == pytest.ExitCode.NO_TESTS_COLLECTED
    assert "1 deselected" in result.stdout
    assert "live node executed" not in result.stdout + result.stderr


def test_explicit_marker_selection_without_cli_gate_is_refused_even_with_dotenv(tmp_path: Path) -> None:
    result = _run_synthetic_pytest(
        tmp_path,
        "import pytest\npytestmark = pytest.mark.live_provider\n"
        "def test_must_not_execute():\n    pytest.fail('provider boundary reached')\n",
        marker_expression="live_provider",
    )

    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "--run-live-provider is required" in result.stdout + result.stderr
    assert "provider boundary reached" not in result.stdout + result.stderr


@pytest.mark.parametrize("remote_marker", ("live_aws", "slow"))
def test_collection_rejects_remote_service_node_without_live_provider(tmp_path: Path, remote_marker: str) -> None:
    result = _run_synthetic_pytest(
        tmp_path,
        f"import pytest\npytestmark = pytest.mark.{remote_marker}\ndef test_remote_boundary():\n    pass\n",
        marker_expression="live_aws or slow",
        run_live_provider=True,
    )

    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "remote-service test nodes must carry @pytest.mark.live_provider" in result.stdout + result.stderr


def test_workflow_is_manual_default_branch_only_and_least_privilege() -> None:
    workflow = _workflow()

    assert _triggers(workflow) == {"workflow_dispatch": None}
    assert workflow["permissions"] == {"contents": "read"}
    assert "pull_request" not in WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request_target" not in WORKFLOW.read_text(encoding="utf-8")
    assert "inputs:" not in WORKFLOW.read_text(encoding="utf-8")
    assert set(workflow["jobs"]) == set(JOBS_TO_LANES)

    for job_id, domain in JOBS_TO_LANES.items():
        job = workflow["jobs"][job_id]
        assert job["environment"] == f"state-engine-{domain}"
        assert job["name"] == f"{domain} / ${{{{ matrix.case_id }}}}"
        assert job["timeout-minutes"] == 90
        assert job["runs-on"] == [
            "self-hosted",
            "Linux",
            "X64",
            "ephemeral",
            "aws-ecs",
            "postgresql-16-rds",
            "state-engine-live-provider",
        ]
        assert "services" not in job, "a local postgres service cannot stand in for the AWS composition"
        assert job["env"]["STATE_ENGINE_CASE_ID"] == "${{ matrix.case_id }}"
        assert job["env"]["STATE_ENGINE_PROFILE_CASE"] == PROFILE
        assert "Assert protected default-branch baseline" in {step.get("name") for step in job["steps"]}
        baseline = next(step for step in job["steps"] if step.get("name") == "Assert protected default-branch baseline")
        assert "github.event.repository.default_branch" in baseline["run"]
        assert "GITHUB_SHA" in baseline["run"]


def test_workflow_actions_are_full_sha_pinned_and_checkout_exact_baseline() -> None:
    workflow = _workflow()

    for job in workflow["jobs"].values():
        observed: dict[str, str] = {}
        for step in job["steps"]:
            uses = step.get("uses")
            if uses is None:
                continue
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", uses), uses
            action, sha = uses.rsplit("@", 1)
            observed[action] = sha
            if action == "actions/checkout":
                assert step["with"] == {"ref": "${{ github.sha }}", "persist-credentials": False}
        assert observed == ACTION_PINS


def test_each_job_selects_exact_lane_nodes_and_fails_closed_before_upload() -> None:
    workflow = _workflow()

    for job in workflow["jobs"].values():
        run = _run_blocks(job)
        assert SELECTORS in run
        assert "scripts/state_engine_live_provider_artifact.py run-pytest" in run
        assert "scripts/state_engine_live_provider_artifact.py seal" in run
        assert 'pytest_status="$?"' in run
        assert '--pytest-exit-code "$pytest_status"' in run
        assert '> "$RUNNER_TEMP/pytest.stdout"' in run
        assert '2> "$RUNNER_TEMP/pytest.stderr"' in run
        assert "tee " not in run
        assert "|| true" not in run
        assert "pytest tests/" not in run
        assert "aws ecs run-task" not in run, "the protected runner is already the maintained ECS composition"

        upload = next(step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/upload-artifact@"))
        assert upload["with"]["name"] == "state-engine-live-${{ steps.lane.outputs.lane_id }}"
        assert upload["with"]["path"] == "${{ runner.temp }}/state-engine-evidence"
        assert upload["with"]["if-no-files-found"] == "error"
        assert 1 <= upload["with"]["retention-days"] <= 7


def test_workflow_never_echoes_secrets_or_uploads_raw_provider_streams() -> None:
    workflow = _workflow()
    forbidden = ("printenv", "set -x", "cat .env", "pytest.stdout", "pytest.stderr")

    for job in workflow["jobs"].values():
        upload = next(step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/upload-artifact@"))
        assert upload["with"]["path"].endswith("state-engine-evidence")
        for step in job["steps"]:
            run = str(step.get("run", ""))
            assert "${{ secrets." not in run
            assert not any(token in run for token in forbidden if token not in {"pytest.stdout", "pytest.stderr"})
        assert "pytest.stdout" not in upload["with"]["path"]
        assert "pytest.stderr" not in upload["with"]["path"]


def test_artifact_sealer_contract_names_the_only_uploadable_files() -> None:
    from scripts.state_engine_live_provider_artifact import ARTIFACT_FILENAMES

    assert ARTIFACT_FILENAMES == EXACT_ARTIFACT_FILES


def test_workflow_matrix_is_the_exact_collision_free_external_case_inventory() -> None:
    from scripts.state_engine_live_provider_artifact import EXTERNAL_CASE_IDS_BY_JOB, lane_id_for_case

    workflow = _workflow()
    observed_lane_ids: set[str] = set()
    for job_id, expected_case_ids in EXTERNAL_CASE_IDS_BY_JOB.items():
        job = workflow["jobs"][job_id]
        observed_case_ids = job["strategy"]["matrix"]["case_id"]
        assert observed_case_ids == list(expected_case_ids)
        for case_id in observed_case_ids:
            lane_id = lane_id_for_case(case_id)
            assert lane_id.startswith("protected-live-")
            assert lane_id not in observed_lane_ids
            observed_lane_ids.add(lane_id)
    assert len(observed_lane_ids) == 38


def _selector(path: Path) -> None:
    path.write_text(
        """{
  "schema_version": 1,
  "catalog_id": "elspeth-state-engine-v3",
  "lanes": [
    {
      "lane_id": "protected-live-source-aws_s3-default",
      "workflow_job": "live-aws / source:aws_s3@default",
      "profile_case": "postgresql-16-aws-single-leader-landscape",
      "marker_expression": "live_provider and live_aws",
      "node_ids": [
        "tests/integration/plugins/sources/test_aws_s3_source_live.py::test_real_s3_default_chain_round_trip[csv]"
      ],
      "cells": [],
      "resource_variables": ["AWS_REGION", "ELSPETH_TEST_S3_BUCKET"],
      "artifact_kinds": ["manifest", "junit_xml", "profile_report", "node_index", "scrub_report"]
    }
  ]
}
""",
        encoding="utf-8",
    )


def _write_successful_raw_artifacts(artifact_dir: Path, selector: Path) -> tuple[Path, Path, Path, Path]:
    from scripts.state_engine_live_provider_artifact import pytest_argv_for_lane

    artifact_dir.mkdir()
    node_id = "tests/integration/plugins/sources/test_aws_s3_source_live.py::test_real_s3_default_chain_round_trip[csv]"
    (artifact_dir / "junit.xml").write_text(
        f'<testsuites tests="1" failures="0" errors="0" skipped="0"><testsuite><testcase name="{node_id}" /></testsuite></testsuites>',
        encoding="utf-8",
    )
    (artifact_dir / "profile.json").write_text(
        """{
  "schema_version": 2,
  "producer": "elspeth-state-engine-profile-reporter-v2",
  "profile_case_id": "postgresql-16-aws-single-leader-landscape",
  "node_ids": ["tests/integration/plugins/sources/test_aws_s3_source_live.py::test_real_s3_default_chain_round_trip[csv]"],
  "outcomes": [{"node_id": "tests/integration/plugins/sources/test_aws_s3_source_live.py::test_real_s3_default_chain_round_trip[csv]", "outcome": "passed"}],
  "warnings": []
}
""",
        encoding="utf-8",
    )
    stdout = artifact_dir.parent / "pytest.stdout"
    stderr = artifact_dir.parent / "pytest.stderr"
    stdout.write_text("provider output is never retained\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    argv_file = artifact_dir.parent / "pytest.argv.json"
    argv = pytest_argv_for_lane(
        selector_path=selector,
        lane_id=TEST_LANE_ID,
        python_executable="/opt/venv/bin/python",
        artifact_dir=artifact_dir,
    )
    argv_file.write_text(__import__("json").dumps(argv), encoding="utf-8")
    return stdout, stderr, argv_file, artifact_dir.parent / "run-metadata.json"


def test_pytest_argv_is_exact_selector_driven_and_strict(tmp_path: Path) -> None:
    from scripts.state_engine_live_provider_artifact import pytest_argv_for_lane

    selector = tmp_path / "selectors.json"
    _selector(selector)
    artifact_dir = tmp_path / "artifact"

    argv = pytest_argv_for_lane(
        selector_path=selector,
        lane_id=TEST_LANE_ID,
        python_executable="/opt/venv/bin/python",
        artifact_dir=artifact_dir,
    )

    assert argv[:4] == ["/opt/venv/bin/python", "-m", "pytest", "-n"]
    assert "--run-live-provider" in argv
    assert argv[argv.index("-p") : argv.index("-p") + 2] == ["-p", "scripts.state_engine_profile_reporter"]
    assert "--state-engine-profile-report" in argv
    assert "--junitxml" in argv
    assert argv[argv.index("-m", 3) + 1] == "live_provider and live_aws"
    assert "-W" in argv and argv[argv.index("-W") + 1] == "error"
    assert argv.count("tests/integration/plugins/sources/test_aws_s3_source_live.py::test_real_s3_default_chain_round_trip[csv]") == 1


def test_sealer_scrubs_canaries_discards_raw_streams_and_writes_exact_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from scripts.state_engine_live_provider_artifact import seal_artifact

    selector = tmp_path / "selectors.json"
    _selector(selector)
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"catalog_id":"elspeth-state-engine-v3"}\n', encoding="utf-8")
    workflow = tmp_path / "workflow.yml"
    workflow.write_text("name: live\n", encoding="utf-8")
    artifact_dir = tmp_path / "artifact"
    stdout, stderr, argv_file, run_metadata = _write_successful_raw_artifacts(artifact_dir, selector)
    started = datetime(2026, 8, 12, 1, 2, 3, tzinfo=UTC)
    run_metadata.write_text(
        json.dumps(
            {
                "started_at": started.isoformat().replace("+00:00", "Z"),
                "ended_at": (started + timedelta(seconds=4)).isoformat().replace("+00:00", "Z"),
                "duration_seconds": 4.0,
                "timeout_seconds": 3600,
            }
        ),
        encoding="utf-8",
    )
    credential_canary = "credential-canary-0123456789"
    provider_canary = "provider-payload-canary-9876543210"
    secret_value = "configured-secret-value-abcdef"
    monkeypatch.setenv("ELSPETH_STATE_ENGINE_CREDENTIAL_CANARY", credential_canary)
    monkeypatch.setenv("ELSPETH_STATE_ENGINE_PROVIDER_PAYLOAD_CANARY", provider_canary)
    monkeypatch.setenv("TEST_PROVIDER_SECRET", secret_value)
    monkeypatch.setenv("GITHUB_REPOSITORY", "johnm-dta/elspeth")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("STATE_ENGINE_RUNNER_IMAGE", "aws-ecs-postgresql16")
    monkeypatch.setenv("STATE_ENGINE_HEAD_SHA", "a" * 40)
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    monkeypatch.setenv("ELSPETH_TEST_S3_BUCKET", "disposable-state-engine-bucket")
    stdout.write_text(stdout.read_text(encoding="utf-8") + secret_value, encoding="utf-8")

    seal_artifact(
        artifact_dir=artifact_dir,
        selector_path=selector,
        lane_id=TEST_LANE_ID,
        catalog_path=catalog,
        workflow_path=workflow,
        stdout_path=stdout,
        stderr_path=stderr,
        argv_path=argv_file,
        run_metadata_path=run_metadata,
        pytest_exit_code=0,
        secret_env_names=("TEST_PROVIDER_SECRET",),
    )

    assert {path.name for path in artifact_dir.iterdir()} == EXACT_ARTIFACT_FILES
    assert not stdout.exists() and not stderr.exists()
    combined = b"".join(path.read_bytes() for path in artifact_dir.iterdir())
    assert credential_canary.encode() not in combined
    assert provider_canary.encode() not in combined
    assert secret_value.encode() not in combined
    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == {
        "schema_version",
        "producer",
        "evidence_id",
        "lane_id",
        "catalog_id",
        "catalog_sha256",
        "repository",
        "workflow_path",
        "workflow_sha256",
        "baseline_sha",
        "run_id",
        "run_attempt",
        "runner_image",
        "ref",
        "head_sha",
        "artifact_name",
        "started_at",
        "ended_at",
        "duration_seconds",
        "timeout_seconds",
        "argv",
        "resources",
        "result_counts",
        "files",
    }
    assert manifest["artifact_name"] == f"state-engine-live-{TEST_LANE_ID}"
    assert manifest["resources"] == ["AWS_REGION", "ELSPETH_TEST_S3_BUCKET"]
    assert set(manifest["files"]) == EXACT_ARTIFACT_FILES - {"manifest.json"}


@pytest.mark.parametrize("outcome", ("skipped", "xfailed", "xpassed", "failed", "error"))
def test_sealer_rejects_every_non_pass_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    import json

    from scripts.state_engine_live_provider_artifact import ArtifactPolicyError, seal_artifact

    selector = tmp_path / "selectors.json"
    _selector(selector)
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"catalog_id":"elspeth-state-engine-v3"}\n', encoding="utf-8")
    workflow = tmp_path / "workflow.yml"
    workflow.write_text("name: live\n", encoding="utf-8")
    artifact_dir = tmp_path / "artifact"
    stdout, stderr, argv_file, run_metadata = _write_successful_raw_artifacts(artifact_dir, selector)
    profile = json.loads((artifact_dir / "profile.json").read_text(encoding="utf-8"))
    profile["outcomes"][0]["outcome"] = outcome
    (artifact_dir / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    run_metadata.write_text(
        json.dumps(
            {
                "started_at": "2026-08-12T01:02:03Z",
                "ended_at": "2026-08-12T01:02:04Z",
                "duration_seconds": 1.0,
                "timeout_seconds": 3600,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ELSPETH_STATE_ENGINE_CREDENTIAL_CANARY", "credential-canary-0123456789")
    monkeypatch.setenv("ELSPETH_STATE_ENGINE_PROVIDER_PAYLOAD_CANARY", "provider-payload-canary-9876543210")
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    monkeypatch.setenv("ELSPETH_TEST_S3_BUCKET", "disposable-state-engine-bucket")

    with pytest.raises(ArtifactPolicyError, match="pass without warnings"):
        seal_artifact(
            artifact_dir=artifact_dir,
            selector_path=selector,
            lane_id=TEST_LANE_ID,
            catalog_path=catalog,
            workflow_path=workflow,
            stdout_path=stdout,
            stderr_path=stderr,
            argv_path=argv_file,
            run_metadata_path=run_metadata,
            pytest_exit_code=0,
            secret_env_names=(),
        )
