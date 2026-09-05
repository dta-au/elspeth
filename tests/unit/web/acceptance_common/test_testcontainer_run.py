"""The ``testcontainer-run`` receipt (6b-4 option (b), elspeth-cb993235e4 / elspeth-d8749aeaa3).

One shared reader, one validator, two provider bindings; the selection pinned to
CI's testcontainer job; every field required from the first receipt; a failing
run is recorded with its exit code, never refused.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from elspeth.web._acceptance_common import testcontainer_run as tr
from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.receipt_validation import _sha256
from elspeth.web._aws_ecs_acceptance import receipt_contracts

REPO_ROOT = Path(__file__).resolve().parents[4]
CANDIDATE = "c" * 40
RECORDED_AT = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)


def _junit(outcomes: list[str | None]) -> bytes:
    cases = []
    for index, outcome in enumerate(outcomes):
        child = "" if outcome is None else f'<{outcome} message="m"/>'
        cases.append(f'<testcase classname="tests.testcontainer.t" name="test_{index}" time="0.1">{child}</testcase>')
    return f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="{len(outcomes)}">{"".join(cases)}</testsuite></testsuites>'.encode()


JUNIT = _junit([None, "failure", "skipped", "error", None])
RECORD = tr.parse_junit_report(JUNIT)


def _receipt(provider: tr.Provider = "aws", *, exit_code: int = 1, record: tr.TestcontainerRunRecord = RECORD) -> dict[str, object]:
    return tr.build_testcontainer_run_receipt(
        provider=provider,
        candidate_sha=CANDIDATE,
        scenario_id="A",
        exit_code=exit_code,
        record=record,
        recorded_at=RECORDED_AT,
    )


def _validate(payload: object, *, provider: tr.Provider = "aws", **overrides: str) -> dict[str, object]:
    kwargs = {"candidate_sha": CANDIDATE, "scenario_id": "A", "subject_sha256": _sha256(RECORD.junit_sha256.encode())}
    kwargs.update(overrides)
    return tr.validate_testcontainer_run_receipt(payload, provider=provider, **kwargs)


def test_selection_is_ci_testcontainer_job_token_for_token() -> None:
    """The receipt can only record the run CI itself requires (P6-CI option (a)); the pin test there is untouched."""
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8"))
    commands = "\n".join(step.get("run", "") for step in workflow["jobs"]["testcontainer"]["steps"])
    for token in tr.TESTCONTAINER_SELECTION:
        assert token in commands, token
    assert tr.TESTCONTAINER_SELECTION[0] == "tests/"
    assert "pytest tests/testcontainer/" not in commands
    assert tr.TESTCONTAINER_SELECTION[tr.TESTCONTAINER_SELECTION.index("-n") + 1] == "0"


def test_parse_junit_report_counts_ids_by_outcome() -> None:
    assert (RECORD.collected, RECORD.passed, RECORD.failed, RECORD.errors, RECORD.skipped) == (5, 2, 1, 1, 1)
    assert RECORD.junit_sha256 == _sha256(JUNIT)


def test_parse_junit_report_rejects_malformed_reports() -> None:
    two_outcomes = _junit([None]).replace(b"</testcase>", b'<failure message="a"/><error message="b"/></testcase>')
    for content in (
        b"",
        b"not xml at all",
        b"<root><testcase/></root>",
        b"<testsuites/>",
        b'<!DOCTYPE x [<!ENTITY e "e">]><testsuites><testcase/></testsuites>',
        b'<testsuites><!ENTITY e "e"><testcase/></testsuites>',
        two_outcomes,
        b"<testsuites>" + b"<testcase/>" * 1 + b"x" * tr.MAX_JUNIT_BYTES,
    ):
        with pytest.raises(AcceptanceCheckError, match="testcontainer_junit"):
            tr.parse_junit_report(content=content)
    with pytest.raises(AcceptanceCheckError, match="testcontainer_junit"):
        tr.parse_junit_report(content=JUNIT.decode())  # type: ignore[arg-type]


def test_read_junit_report_bounds_the_file(tmp_path: Path) -> None:
    report = tmp_path / "testcontainer-junit.xml"
    report.write_bytes(JUNIT)
    assert tr.read_junit_report(report) == RECORD
    for path in (tmp_path, tmp_path / "missing.xml"):
        with pytest.raises(AcceptanceCheckError, match="testcontainer_junit"):
            tr.read_junit_report(path)


def test_record_refuses_inconsistent_counts() -> None:
    with pytest.raises(ValueError):
        tr.TestcontainerRunRecord(collected=2, passed=1, failed=0, errors=0, skipped=0, junit_sha256="a" * 64)
    with pytest.raises(ValueError):
        tr.TestcontainerRunRecord(collected=1, passed=True, failed=0, errors=0, skipped=0, junit_sha256="a" * 64)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        tr.TestcontainerRunRecord(collected=1, passed=1, failed=0, errors=0, skipped=0, junit_sha256="A" * 64)


@pytest.mark.parametrize("provider", ["aws", "azure"])
def test_build_receipt_validates_under_its_own_binding_only(provider: tr.Provider) -> None:
    receipt = _receipt(provider)
    assert receipt["schema"] == tr.TESTCONTAINER_RUN_SCHEMAS[provider]
    assert set(receipt) == tr._RECEIPT_FIELDS
    assert receipt["selection"] == list(tr.TESTCONTAINER_SELECTION)
    assert _validate(receipt, provider=provider) is receipt
    other: tr.Provider = "azure" if provider == "aws" else "aws"
    with pytest.raises(AcceptanceCheckError, match="receipt_store_schema"):
        _validate(receipt, provider=other)


def test_a_failing_run_is_recorded_with_its_exit_code_not_refused() -> None:
    assert _receipt(exit_code=1)["exit_code"] == 1
    clean = tr.parse_junit_report(_junit([None, None, "skipped"]))
    assert _receipt(exit_code=0, record=clean)["failed"] == 0
    # Exit 0 cannot coexist with failures/errors, and a failing count needs a non-zero exit.
    with pytest.raises(AcceptanceCheckError, match="receipt_store_schema"):
        _receipt(exit_code=0)
    with pytest.raises(AcceptanceCheckError, match="receipt_store_schema"):
        _receipt(exit_code=1, record=clean)


def test_validate_testcontainer_run_receipt_rejects_open_or_inconsistent_receipts() -> None:
    base = _receipt()
    schema_mutations: list[dict[str, object]] = [
        {**base, "extra": 1},
        {key: value for key, value in base.items() if key != "skipped"},
        {**base, "kind": "verify-s3"},
        {**base, "selection": ["tests/testcontainer/", "-m", "testcontainer", "-n", "0", "--junitxml=testcontainer-junit.xml"]},
        {**base, "selection": list(tr.TESTCONTAINER_SELECTION)[:-1]},
        {**base, "selection": " ".join(tr.TESTCONTAINER_SELECTION)},
        {**base, "exit_code": True},
        {**base, "exit_code": 256},
        {**base, "exit_code": "1"},
        {**base, "collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
        {**base, "collected": 6},
        {**base, "passed": 2.0},
        {**base, "exit_code": 0},
        {**base, "junit_sha256": base["junit_sha256"].upper()},  # type: ignore[union-attr]
        {**base, "junit_sha256": "a" * 63},
        {**base, "recorded_at": "2026-09-05T06:00:00+00:00"},
        {**base, "recorded_at": "yesterday"},
    ]
    for mutated in schema_mutations:
        with pytest.raises(AcceptanceCheckError, match="receipt_store_schema"):
            tr.validate_testcontainer_run_receipt(
                payload=mutated,
                provider="aws",
                candidate_sha=CANDIDATE,
                scenario_id="A",
                subject_sha256=_sha256(RECORD.junit_sha256.encode()),
            )
    with pytest.raises(AcceptanceCheckError, match="receipt_store_schema"):
        tr.validate_testcontainer_run_receipt(
            payload=["not", "a", "dict"], provider="aws", candidate_sha=CANDIDATE, scenario_id="A", subject_sha256="0" * 64
        )
    for overrides in ({"candidate_sha": "d" * 40}, {"scenario_id": "B"}, {"subject_sha256": "0" * 64}):
        with pytest.raises(AcceptanceCheckError, match="receipt_store_binding"):
            _validate(base, **overrides)
    with pytest.raises(AcceptanceInputError):
        _validate(base, provider="gcp")  # type: ignore[arg-type]


def test_build_receipt_refuses_inputs_the_validator_would_refuse() -> None:
    with pytest.raises(AcceptanceInputError):
        tr.build_testcontainer_run_receipt(
            provider="gcp", candidate_sha=CANDIDATE, scenario_id="A", exit_code=0, record=RECORD, recorded_at=RECORDED_AT
        )  # type: ignore[arg-type]
    with pytest.raises(AcceptanceInputError):
        tr.build_testcontainer_run_receipt(
            provider="aws", candidate_sha="not-a-sha", scenario_id="A", exit_code=0, record=RECORD, recorded_at=RECORDED_AT
        )
    with pytest.raises(AcceptanceInputError):
        tr.build_testcontainer_run_receipt(
            provider="aws", candidate_sha=CANDIDATE, scenario_id="A", exit_code=True, record=RECORD, recorded_at=RECORDED_AT
        )
    with pytest.raises(AcceptanceInputError):
        tr.build_testcontainer_run_receipt(
            provider="aws",
            candidate_sha=CANDIDATE,
            scenario_id="A",
            exit_code=1,
            record=RECORD,
            recorded_at=RECORDED_AT.replace(tzinfo=None),
        )


def test_ecs_binding_accepts_the_aws_receipt_and_refuses_the_azure_one() -> None:
    """The ECS store admits the kind through the shared validator under its own schema id."""
    assert tr.TESTCONTAINER_RUN_RECEIPT_KIND in receipt_contracts._RECEIPT_KINDS
    aws = _receipt("aws")
    subject_sha256 = _sha256(RECORD.junit_sha256.encode())
    accepted = receipt_contracts._validate_stored_receipt(
        aws, kind=tr.TESTCONTAINER_RUN_RECEIPT_KIND, scenario_id="A", subject_sha256=subject_sha256, candidate_sha=CANDIDATE
    )
    assert accepted is aws
    with pytest.raises(AcceptanceCheckError, match="receipt_store_schema"):
        receipt_contracts._validate_stored_receipt(
            _receipt("azure"),
            kind=tr.TESTCONTAINER_RUN_RECEIPT_KIND,
            scenario_id="A",
            subject_sha256=subject_sha256,
            candidate_sha=CANDIDATE,
        )
    with pytest.raises(AcceptanceCheckError, match="receipt_store_binding"):
        receipt_contracts._validate_stored_receipt(
            aws, kind=tr.TESTCONTAINER_RUN_RECEIPT_KIND, scenario_id="B", subject_sha256=subject_sha256, candidate_sha=CANDIDATE
        )


def test_command_emits_a_storable_receipt_and_mirrors_its_input_failures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = tmp_path / "testcontainer-junit.xml"
    report.write_bytes(JUNIT)
    argv = ["--provider", "aws", "--junit", str(report), "--exit-code", "1", "--candidate-sha", CANDIDATE, "--scenario-id", "A"]
    assert tr.main(argv) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert _validate(receipt) is receipt
    assert receipt["junit_sha256"] == RECORD.junit_sha256
    assert (
        tr.main(
            [
                "--provider",
                "azure",
                "--junit",
                str(tmp_path / "missing.xml"),
                "--exit-code",
                "0",
                "--candidate-sha",
                CANDIDATE,
                "--scenario-id",
                "A",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out) == {"receipt": "testcontainer-run", "error": "testcontainer_junit"}
    assert (
        tr.main(["--provider", "aws", "--junit", str(report), "--exit-code", "0", "--candidate-sha", CANDIDATE, "--scenario-id", "A"]) == 2
    )
    assert json.loads(capsys.readouterr().out)["error"] == "receipt_store_schema"
    assert tr.main(["--provider", "aws", "--junit", str(report), "--exit-code", "1", "--candidate-sha", "nope", "--scenario-id", "A"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "input_invalid"


def test_module_is_executable_with_the_running_interpreter(tmp_path: Path) -> None:
    report = tmp_path / "testcontainer-junit.xml"
    report.write_bytes(JUNIT)
    source_roots = os.pathsep.join(str(REPO_ROOT / part) for part in ("src", "elspeth-lints/src"))
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "elspeth.web._acceptance_common.testcontainer_run",
            "--provider",
            "azure",
            "--junit",
            str(report),
            "--exit-code",
            "1",
            "--candidate-sha",
            CANDIDATE,
            "--scenario-id",
            "A",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={"PYTHONPATH": source_roots, "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["schema"] == tr.TESTCONTAINER_RUN_SCHEMAS["azure"]
