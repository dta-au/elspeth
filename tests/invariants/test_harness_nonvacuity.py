"""Negative controls for pass-through probe coverage and registry isolation."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from elspeth.contracts import TransformResult
from elspeth.plugins.transforms.passthrough import PassThrough
from tests.invariants import test_pass_through_invariants as harness


@pytest.mark.parametrize("outcome", ["error", "empty", "empty_input"])
def test_forward_harness_rejects_uncheckable_probes(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    if outcome == "empty_input":
        monkeypatch.setattr(PassThrough, "forward_invariant_probe_rows", lambda self, row: [])
    elif outcome == "error":
        monkeypatch.setattr(
            PassThrough,
            "execute_forward_invariant_probe",
            lambda self, rows, ctx: TransformResult.error({"reason": "validation_failed"}),
        )
    else:
        monkeypatch.setattr(
            PassThrough,
            "execute_forward_invariant_probe",
            lambda self, rows, ctx: TransformResult.success_empty(success_reason={"action": "filter"}),
        )
    with pytest.raises(AssertionError, match="checkable"):
        harness.test_annotated_transforms_preserve_input_fields(_annotated_cls=PassThrough)


def test_forward_harness_accepts_truthful_probe() -> None:
    harness.test_annotated_transforms_preserve_input_fields(_annotated_cls=PassThrough)


@pytest.mark.parametrize("mutation", ["restored", "plugin", "declaration", "teardown"])
def test_registry_guard_checks_after_fixture_restoration(tmp_path: Path, mutation: str) -> None:
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "conftest.py").write_text(
        "from tests.invariants.conftest import _verify_plugin_manager_clean\nfrom tests.invariants.conftest import monkeypatch\n"
    )
    bodies = {
        "restored": "monkeypatch.setattr(manager, 'get_transforms', lambda: [])",
        "plugin": "manager._transforms.clear()",
        "declaration": "declarations._clear_registry_for_tests()",
        "teardown": "request.addfinalizer(manager._transforms.clear)",
    }
    (tmp_path / "test_probe.py").write_text(
        "from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager\n"
        "from elspeth.contracts import declaration_contracts as declarations\n"
        "import elspeth.engine.executors.pass_through\n"
        "def test_probe(monkeypatch, request):\n"
        "    manager = get_shared_plugin_manager()\n"
        f"    {bodies[mutation]}\n"
    )
    env = os.environ | {"PYTHONPATH": os.pathsep.join([str(root), str(root / "src"), str(root / "elspeth-lints/src")])}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-n", "0", "-o", "addopts=", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr
    if mutation == "restored":
        assert result.returncode == 0, output
    else:
        assert result.returncode == 1, output
        assert "registry changed" in output, output
