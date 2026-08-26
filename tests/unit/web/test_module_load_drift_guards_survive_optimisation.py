"""Module-load drift guards must still fire under ``python -O``.

``python -O`` strips every ``assert`` statement from compiled code. A drift
guard written as a bare ``assert`` is therefore a guard that DOES NOT EXIST in
an optimised deployment — while passing every CI run, every local run and every
mutation test, because none of those use ``-O``. The shape is invisible to
normal testing, so nobody finds it by working (elspeth-37941f1731).

The two guards pinned here were the census's TIER 1: sole enforcement, with no
test consuming the guarded constant, so when the assertion vanished nothing
else caught the drift.

* ``audit_readiness.service`` — a stale member in
  ``_INTERNAL_TRANSFORM_DETERMINISMS`` silently SHRINKS
  ``_AUDIT_FLAGGED_DETERMINISMS``, so a transform that should be audit-flagged
  stops being flagged. Silent audit-fidelity loss.
* ``composer.guided.prompts`` — a ``GuidedStep`` member missing from
  ``_STEP_FILE_NAMES`` silently drops that step from the skill handed to the
  planner on every guided turn. That module's own comment promised to "fail
  loudly at import time rather than silently omit the step"; as an ``assert``
  it did the opposite under ``-O``.

WHY THESE TESTS RUN A SUBPROCESS. The guard fires at MODULE LOAD, and the
failure mode is specific to ``-O``, which cannot be toggled inside a running
interpreter (``sys.flags.optimize`` is read-only and the parent pytest process
is unoptimised). Importing the module in-process would therefore test the one
configuration that was never broken. Each test compiles a patched copy of the
real module under a real ``-O`` interpreter and asserts the failure, which is
what makes the pin fail if someone converts a ``raise`` back to an ``assert``.

WHY THE DRIFT IS INJECTED RATHER THAN ASSERTED ABOUT. A test that merely reads
the source and asserts "this module contains no bare assert" pins a spelling,
not a behaviour: it passes against a guard that raises the wrong exception,
compares the wrong things, or has been reduced to ``if False``. These tests
inject the exact drift each guard exists to catch and require the import to
fail — the consequence, not the mechanism.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import elspeth

_SRC_ROOT = Path(elspeth.__file__).resolve().parent.parent


def _run_patched_module_under_O(module_path: Path, old: str, new: str) -> subprocess.CompletedProcess[str]:
    """Import ``module_path``'s source with ``old`` replaced by ``new``, under ``-O``.

    The module source is read from the live tree, patched in memory, and
    executed under a genuinely optimised interpreter. Nothing on disk is
    modified — this runs in a shared checkout.

    The patched source is executed under the module's REAL package name via
    ``importlib``, so its relative imports and package-level state resolve
    exactly as they do in production rather than as an orphaned script.
    """
    source = module_path.read_text()
    if old not in source:
        pytest.fail(
            f"drift-injection anchor not found in {module_path.name}; the guard or the constant it "
            f"protects has been rewritten, so this test is no longer injecting the drift it names. "
            f"Anchor: {old!r}"
        )
    patched = source.replace(old, new, 1)

    module_name = "elspeth." + str(module_path.relative_to(_SRC_ROOT / "elspeth")).removesuffix(".py").replace("/", ".")
    driver = textwrap.dedent(
        """
        import importlib.util, sys
        assert sys.flags.optimize > 0, "probe must run optimised or it proves nothing"
        try:
            assert False  # noqa: B011, PT015
        except AssertionError:  # pragma: no cover - would mean -O was not honoured
            raise SystemExit("assert statements are still live; -O was not honoured")
        source = sys.stdin.read()
        spec = importlib.util.spec_from_file_location(sys.argv[1], sys.argv[2])
        module = importlib.util.module_from_spec(spec)
        sys.modules[sys.argv[1]] = module
        exec(compile(source, sys.argv[2], "exec"), module.__dict__)
        print("IMPORTED-CLEAN")
        """
    )
    return subprocess.run(
        [sys.executable, "-O", "-c", driver, module_name, str(module_path)],
        input=patched,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(_SRC_ROOT.parent),
    )


def test_the_probe_itself_runs_optimised() -> None:
    """Guard the guard: if ``-O`` were not honoured, both tests below would pass vacuously.

    They would pass because the ORIGINAL assert-based guards also fire when
    assertions are live — the whole defect is that they fire only then. So the
    probe asserting its own optimisation is what makes a failure below
    attributable to the guard rather than to the harness.
    """
    result = subprocess.run(
        [sys.executable, "-O", "-c", "import sys; print(sys.flags.optimize)"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != "0", "the -O interpreter reports no optimisation; these tests cannot discriminate"


def test_audit_flagged_determinism_drift_still_fails_under_optimisation() -> None:
    """A stale determinism exclusion must not silently un-flag an audited transform."""
    module_path = _SRC_ROOT / "elspeth" / "web" / "audit_readiness" / "service.py"
    result = _run_patched_module_under_O(
        module_path,
        old="        Determinism.IO_WRITE,\n",
        new='        Determinism.IO_WRITE,\n        "stale_determinism_that_drifted",  # type: ignore[arg-type]\n',
    )

    assert result.returncode != 0, (
        "audit_readiness.service imported CLEAN under -O with a stale member in "
        "_INTERNAL_TRANSFORM_DETERMINISMS. The drift guard is not firing when optimised — it has "
        "most likely been converted back to a bare `assert`, which -O strips. "
        f"stdout={result.stdout!r}"
    )
    assert "stale_determinism_that_drifted" in result.stderr, (
        f"the import failed, but not with the drift guard's own message naming the stale member; "
        f"the failure may be unrelated. stderr={result.stderr!r}"
    )


def test_guided_step_coverage_drift_still_fails_under_optimisation() -> None:
    """A GuidedStep with no skill file must not be silently dropped from the composed skill."""
    module_path = _SRC_ROOT / "elspeth" / "web" / "composer" / "guided" / "prompts.py"
    result = _run_patched_module_under_O(
        module_path,
        old='    GuidedStep.STEP_4_WIRE: "step_4_wire.md",\n',
        new="",
    )

    assert result.returncode != 0, (
        "composer.guided.prompts imported CLEAN under -O with STEP_4_WIRE missing from "
        "_STEP_FILE_NAMES. That step would be silently omitted from the skill handed to the planner "
        "on every guided turn — the exact outcome the guard's own comment promises to prevent. "
        f"stdout={result.stdout!r}"
    )
    assert "STEP_4_WIRE" in result.stderr, (
        f"the import failed, but not with the drift guard's own message naming the missing step; "
        f"the failure may be unrelated. stderr={result.stderr!r}"
    )
