"""The rollback-refusal gate: jq parity over one corpus, fail-closed on shape, the CLI's exit codes.

``test_gate_verdicts_match_the_ecs_runbook_jq_filter`` is the parity test plan
§6.1 item 5 demands: one corpus (the two pre-extraction compatibility records,
mutated field by field) through the runbook's own ``jq -e`` filter and through
:func:`compatibility_record_gate`, identical verdicts. The jq filter is read
from the runbook exactly as ``tests/unit/docs/test_release_version_surfaces.py``
reads it, so the runbook stays the ECS authority and this predicate is proven
against it rather than beside it.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web._acceptance_common import compatibility_gate
from elspeth.web._acceptance_common.compatibility_gate import (
    GATE_CLAUSES,
    CompatibilityGateVerdict,
    compatibility_record_gate,
)
from elspeth.web._acceptance_common.errors import AcceptanceInputError
from elspeth.web._acceptance_common.schema_facts import _ROLLBACK_BASELINE_LANDSCAPE_EPOCH, _expected_schema_facts
from tests.unit.docs.test_release_version_surfaces import _rollback_refusal_jq_filter, _run_jq

CORPUS = json.loads((Path(__file__).parent / "corpus" / "ecs_receipts.json").read_text(encoding="utf-8"))
JQ_PRESENT = shutil.which("jq") is not None


def _record(scenario_id: str) -> dict[str, object]:
    # The gate reads the live derivation; refresh the recorded facts so the
    # corpus keeps exercising the predicate after a schema-epoch bump.
    record = copy.deepcopy(CORPUS["compatibility_records"][scenario_id]["record"])
    record["schema_facts"] = _expected_schema_facts(scenario_id)
    return record


def _mutations(scenario_id: str) -> list[tuple[str, dict[str, object]]]:
    base = _record(scenario_id)
    cases: list[tuple[str, dict[str, object]]] = [("unchanged", base)]

    def mutate(label: str, apply: object) -> None:
        candidate = copy.deepcopy(base)
        apply(candidate)  # type: ignore[operator]
        cases.append((label, candidate))

    mutate("backward_compatible_true", lambda r: r.__setitem__("backward_compatible", True))
    mutate("backward_compatible_zero", lambda r: r.__setitem__("backward_compatible", 0))
    mutate("backward_compatible_string", lambda r: r.__setitem__("backward_compatible", "false"))
    mutate("backward_compatible_missing", lambda r: r.pop("backward_compatible"))
    mutate("rollback_permitted_true", lambda r: r.__setitem__("rollback_permitted", True))
    mutate("rollback_permitted_null", lambda r: r.__setitem__("rollback_permitted", None))
    mutate("rollback_permitted_missing", lambda r: r.pop("rollback_permitted"))
    mutate("candidate_epoch_plus_one", lambda r: r["schema_facts"]["candidate"].__setitem__("landscape_epoch", SQLITE_SCHEMA_EPOCH + 1))
    mutate("candidate_epoch_as_float", lambda r: r["schema_facts"]["candidate"].__setitem__("landscape_epoch", float(SQLITE_SCHEMA_EPOCH)))
    mutate("candidate_epoch_as_string", lambda r: r["schema_facts"]["candidate"].__setitem__("landscape_epoch", str(SQLITE_SCHEMA_EPOCH)))
    mutate("candidate_epoch_as_bool", lambda r: r["schema_facts"]["candidate"].__setitem__("landscape_epoch", True))
    mutate("candidate_missing", lambda r: r["schema_facts"].pop("candidate"))
    mutate("schema_facts_missing", lambda r: r.pop("schema_facts"))
    mutate("schema_facts_null", lambda r: r.__setitem__("schema_facts", None))
    mutate("schema_facts_list", lambda r: r.__setitem__("schema_facts", []))
    mutate("session_epoch_drift_ignored", lambda r: r["schema_facts"]["candidate"].__setitem__("session_epoch", 1))
    mutate("forward_compatible_false_ignored", lambda r: r.__setitem__("forward_compatible", False))
    mutate("decision_rejected_ignored", lambda r: r.__setitem__("decision", "rejected"))
    if scenario_id == "B":
        mutate(
            "previous_epoch_plus_one",
            lambda r: r["schema_facts"]["previous"].__setitem__("landscape_epoch", _ROLLBACK_BASELINE_LANDSCAPE_EPOCH + 1),
        )
        mutate("previous_null", lambda r: r["schema_facts"].__setitem__("previous", None))
        mutate("previous_missing", lambda r: r["schema_facts"].pop("previous"))
        mutate("previous_epoch_as_bool", lambda r: r["schema_facts"]["previous"].__setitem__("landscape_epoch", True))
    return cases


@pytest.mark.skipif(not JQ_PRESENT, reason="jq is the runbook's own gate executable; the parity claim cannot be made without it")
@pytest.mark.parametrize(("label", "record"), _mutations("B"), ids=[label for label, _ in _mutations("B")])
def test_gate_verdicts_match_the_ecs_runbook_jq_filter(label: str, record: dict[str, object]) -> None:
    query = _rollback_refusal_jq_filter()
    jq_passed = _run_jq(query, record).returncode == 0
    verdict = compatibility_record_gate(record, scenario_id="B")
    assert verdict.passed == jq_passed, (label, verdict)


def test_runbook_filter_still_tests_exactly_the_four_clauses() -> None:
    """If the runbook's jq gate ever grows a clause, the parity corpus must grow with it."""
    query = _rollback_refusal_jq_filter()
    lines = [line.strip() for line in query.strip().splitlines()]
    assert lines == [
        ".backward_compatible == false",
        "and .rollback_permitted == false",
        f"and .schema_facts.previous.landscape_epoch == {_ROLLBACK_BASELINE_LANDSCAPE_EPOCH}",
        f"and .schema_facts.candidate.landscape_epoch == {SQLITE_SCHEMA_EPOCH}",
    ]


@pytest.mark.parametrize(("label", "record"), _mutations("A"), ids=[label for label, _ in _mutations("A")])
def test_scenario_a_gate_demands_a_null_previous_release(label: str, record: dict[str, object]) -> None:
    verdict = compatibility_record_gate(record, scenario_id="A")
    # jq compares numbers by value (37.0 == 37), so the float form passes; the
    # string and boolean forms do not.
    expected_pass = label in {
        "unchanged",
        "candidate_epoch_as_float",
        "session_epoch_drift_ignored",
        "forward_compatible_false_ignored",
        "decision_rejected_ignored",
    }
    assert verdict.passed == expected_pass, (label, verdict)


def test_scenario_a_record_fails_the_scenario_b_gate_and_vice_versa() -> None:
    assert compatibility_record_gate(_record("A"), scenario_id="B").failed_clauses == ("previous_landscape_epoch",)
    assert compatibility_record_gate(_record("B"), scenario_id="A").failed_clauses == ("previous_landscape_epoch",)


@pytest.mark.parametrize(
    ("record", "expected_failed"),
    [
        pytest.param(None, GATE_CLAUSES, id="null"),
        pytest.param([], GATE_CLAUSES, id="list"),
        pytest.param("record", GATE_CLAUSES, id="string"),
        pytest.param({}, GATE_CLAUSES, id="empty"),
        pytest.param({"backward_compatible": 0, "rollback_permitted": "false"}, GATE_CLAUSES, id="type-confused-booleans"),
        pytest.param(
            {"backward_compatible": False, "rollback_permitted": False, "schema_facts": {"candidate": {"landscape_epoch": True}}},
            ("previous_landscape_epoch", "candidate_landscape_epoch"),
            id="bool-is-not-an-epoch",
        ),
        pytest.param(
            {
                "backward_compatible": False,
                "rollback_permitted": False,
                "schema_facts": {"candidate": {"landscape_epoch": SQLITE_SCHEMA_EPOCH}},
            },
            ("previous_landscape_epoch",),
            id="previous-missing-under-scenario-b",
        ),
    ],
)
def test_gate_fails_closed_on_missing_paths_and_type_confusion(record: object, expected_failed: tuple[str, ...]) -> None:
    verdict = compatibility_record_gate(record, scenario_id="B")
    assert not verdict.passed
    assert verdict.failed_clauses == tuple(expected_failed)


def test_gate_refuses_an_unknown_scenario() -> None:
    with pytest.raises(AcceptanceInputError):
        compatibility_record_gate(_record("A"), scenario_id="C")


def test_verdict_is_internally_consistent() -> None:
    with pytest.raises(ValueError):
        CompatibilityGateVerdict(passed=True, failed_clauses=("rollback_permitted",))
    with pytest.raises(ValueError):
        CompatibilityGateVerdict(passed=False, failed_clauses=())
    with pytest.raises(ValueError):
        CompatibilityGateVerdict(passed=False, failed_clauses=("not_a_clause",))


class TestCommand:
    def _write(self, tmp_path: Path, record: object, *, mode: int = 0o600) -> Path:
        path = tmp_path / "receipt.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        os.chmod(path, mode)
        return path

    def test_passing_record_exits_zero_and_prints_the_verdict(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = self._write(tmp_path, _record("B"))
        assert compatibility_gate.main(["--record", str(path), "--scenario-id", "B"]) == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed == {"gate": "compatibility-record-gate", "scenario_id": "B", "passed": True, "failed_clauses": []}

    def test_failing_record_exits_one_and_names_the_clauses_only(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        record = _record("A")
        record["rollback_permitted"] = True
        path = self._write(tmp_path, record)
        assert compatibility_gate.main(["--record", str(path), "--scenario-id", "A"]) == 1
        out = capsys.readouterr().out
        assert json.loads(out) == {
            "gate": "compatibility-record-gate",
            "scenario_id": "A",
            "passed": False,
            "failed_clauses": ["rollback_permitted"],
        }
        assert "approver" not in out

    def test_unprotected_record_file_exits_two(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = self._write(tmp_path, _record("A"), mode=0o644)
        assert compatibility_gate.main(["--record", str(path), "--scenario-id", "A"]) == 2
        assert json.loads(capsys.readouterr().out) == {
            "gate": "compatibility-record-gate",
            "passed": False,
            "error": "compatibility_record_file",
        }

    def test_module_is_executable_and_mirrors_jq_exit_codes(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, _record("B"))
        # Spawn the interpreter running this test (sys.executable), never a bare
        # ``python`` from PATH: on a checkout whose shell lacks the venv that
        # resolves to the system interpreter and the import fails on a missing
        # dependency, not on the module under test. The child inherits nothing
        # from the calling shell: cwd is the scratch dir (so the module is found
        # through PYTHONPATH, not through an editable install or the cwd), and
        # the environment is exactly the two source roots plus a default PATH.
        source_roots = os.pathsep.join(str(Path(__file__).resolve().parents[4] / part) for part in ("src", "elspeth-lints/src"))
        completed = subprocess.run(
            [sys.executable, "-m", "elspeth.web._acceptance_common.compatibility_gate", "--record", str(path), "--scenario-id", "B"],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
            env={"PYTHONPATH": source_roots, "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)["passed"] is True
