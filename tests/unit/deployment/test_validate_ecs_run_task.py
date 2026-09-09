"""Focused tests for strict admission of ``aws ecs run-task`` output."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = REPO_ROOT / "deploy" / "aws-ecs" / "terraform" / "scripts" / "validate-ecs-run-task.py"

PARTITION = "aws"
ACCOUNT_ID = "123456789012"
REGION = "ap-southeast-2"
CLUSTER = "acceptance-run-cluster"
TASK_ID = "0123456789abcdef0123456789abcdef"
TASK_ARN = f"arn:{PARTITION}:ecs:{REGION}:{ACCOUNT_ID}:task/{CLUSTER}/{TASK_ID}"
CLUSTER_ARN = f"arn:{PARTITION}:ecs:{REGION}:{ACCOUNT_ID}:cluster/{CLUSTER}"
TASK_DEFINITION_ARN = f"arn:{PARTITION}:ecs:{REGION}:{ACCOUNT_ID}:task-definition/acceptance-bootstrap:7"


def _valid_response() -> dict[str, object]:
    return {
        "failures": [],
        "tasks": [
            {
                "taskArn": TASK_ARN,
                "clusterArn": CLUSTER_ARN,
                "taskDefinitionArn": TASK_DEFINITION_ARN,
                "lastStatus": "PROVISIONING",
            }
        ],
    }


def _run(payload: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--partition",
            PARTITION,
            "--account-id",
            ACCOUNT_ID,
            "--region",
            REGION,
            "--cluster",
            CLUSTER,
            "--task-definition",
            TASK_DEFINITION_ARN,
        ],
        cwd=REPO_ROOT,
        input=payload,
        capture_output=True,
        check=False,
    )


def _encoded(response: object) -> bytes:
    return json.dumps(response, separators=(",", ":")).encode("utf-8")


def _assert_refused(payload: bytes) -> None:
    completed = _run(payload)
    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr.startswith(b"validate-ecs-run-task: refused ")


def test_valid_response_prints_only_the_admitted_task_arn() -> None:
    completed = _run(_encoded(_valid_response()))

    assert completed.returncode == 0
    assert completed.stdout == f"{TASK_ARN}\n".encode()
    assert completed.stderr == b""


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b'{"failures":[],"tasks":[],"tasks":[]}',
        b'{"failures":[],"tasks":[{"score":NaN}]}',
        b"[]",
        b"x" * (1024 * 1024 + 1),
    ],
    ids=["invalid-json", "duplicate-key", "non-finite", "non-object", "oversized"],
)
def test_malformed_responses_are_refused(payload: bytes) -> None:
    _assert_refused(payload)


def test_multiple_tasks_are_refused() -> None:
    response = _valid_response()
    tasks = response["tasks"]
    assert isinstance(tasks, list)
    tasks.append(dict(tasks[0]))

    _assert_refused(_encoded(response))


def test_reported_failure_is_refused_even_when_one_task_is_present() -> None:
    response = _valid_response()
    response["failures"] = [{"arn": TASK_ARN, "reason": "RESOURCE:MEMORY"}]

    _assert_refused(_encoded(response))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        (
            "taskArn",
            f"arn:{PARTITION}:ecs:{REGION}:999999999999:task/{CLUSTER}/{TASK_ID}",
        ),
        (
            "clusterArn",
            f"arn:{PARTITION}:ecs:us-east-1:{ACCOUNT_ID}:cluster/{CLUSTER}",
        ),
        (
            "taskArn",
            f"arn:{PARTITION}:ecs:{REGION}:{ACCOUNT_ID}:task/other-cluster/{TASK_ID}",
        ),
        (
            "taskDefinitionArn",
            f"arn:{PARTITION}:ecs:{REGION}:999999999999:task-definition/acceptance-bootstrap:7",
        ),
    ],
    ids=["cross-account", "cross-region", "cross-cluster", "wrong-task-definition"],
)
def test_cross_boundary_task_identity_is_refused(field: str, replacement: str) -> None:
    response = _valid_response()
    tasks = response["tasks"]
    assert isinstance(tasks, list)
    task = tasks[0]
    assert isinstance(task, dict)
    task[field] = replacement

    _assert_refused(_encoded(response))
