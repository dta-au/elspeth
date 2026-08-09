"""Strict offline comparison of rendered and attached IAM policy actions."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = REPO_ROOT / "deploy" / "aws-ecs" / "terraform" / "scripts" / "verify-iam-policy-actions.py"


def _rendered() -> dict[str, object]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "Read", "Effect": "Allow", "Action": "ec2:DescribeVpcs", "Resource": "*"},
            {
                "Sid": "Write",
                "Effect": "Allow",
                "Action": ["ec2:CreateVpc", "ec2:DeleteVpc"],
                "Resource": "*",
            },
        ],
    }


def _live(document: object | None = None) -> dict[str, object]:
    return {
        "PolicyVersion": {
            "Document": _rendered() if document is None else document,
            "VersionId": "v7",
            "IsDefaultVersion": True,
        }
    }


def _run(rendered: Path, live: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--rendered-policy",
            str(rendered),
            "--live-policy-version",
            str(live),
            "--label",
            "control-plane",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def _assert_refused(completed: subprocess.CompletedProcess[bytes]) -> None:
    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr.startswith(b"verify-iam-policy-actions: refused ")


def test_equal_complete_flattened_action_sets_pass_regardless_of_statement_order(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered.json"
    live = tmp_path / "live.json"
    _write(rendered, _rendered())
    _write(
        live,
        _live(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Action": ["ec2:DeleteVpc", "ec2:DescribeVpcs"], "Resource": "*"},
                    {"Effect": "Allow", "Action": "ec2:CreateVpc", "Resource": "*"},
                ],
            }
        ),
    )

    completed = _run(rendered, live)

    assert completed.returncode == 0
    assert completed.stdout == b"IAM policy action set verified: control-plane\n"
    assert completed.stderr == b""


def test_drift_reports_both_missing_and_unexpected_actions_and_fails(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered.json"
    live = tmp_path / "live.json"
    _write(rendered, _rendered())
    _write(
        live,
        _live(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["ec2:DescribeVpcs", "ec2:CreateSubnet"],
                        "Resource": "*",
                    }
                ],
            }
        ),
    )

    completed = _run(rendered, live)

    _assert_refused(completed)
    assert b"missing_from_live=ec2:CreateVpc,ec2:DeleteVpc" in completed.stderr
    assert b"unexpected_in_live=ec2:CreateSubnet" in completed.stderr


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"Statement": "not-a-list"},
        {"Statement": [{"Effect": "Allow", "NotAction": "ec2:*", "Resource": "*"}]},
        {"Statement": [{"Effect": "Allow", "Action": ["ec2:DescribeVpcs", 7], "Resource": "*"}]},
        {"Statement": [{"Effect": "Deny", "Action": "ec2:DescribeVpcs", "Resource": "*"}]},
    ],
    ids=["missing-statements", "wrong-statement-shape", "not-action", "mixed-actions", "deny-statement"],
)
def test_unsupported_policy_shapes_are_refused(tmp_path: Path, document: object) -> None:
    rendered = tmp_path / "rendered.json"
    live = tmp_path / "live.json"
    _write(rendered, document)
    _write(live, _live())

    _assert_refused(_run(rendered, live))


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b'{"Statement":[],"Statement":[]}',
        b'{"Statement":[{"Effect":"Allow","Action":NaN}]}',
        b"[]",
    ],
    ids=["invalid-json", "duplicate-key", "non-finite", "non-object"],
)
def test_malformed_or_ambiguous_json_is_refused(tmp_path: Path, raw: bytes) -> None:
    rendered = tmp_path / "rendered.json"
    live = tmp_path / "live.json"
    rendered.write_bytes(raw)
    _write(live, _live())

    _assert_refused(_run(rendered, live))
