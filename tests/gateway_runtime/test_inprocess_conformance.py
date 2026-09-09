"""Runs the gateway's own test suites as subprocesses, and checks SDK import isolation.

Two subprocess-run tests give the ELSPETH ``pytest tests/`` gate coverage of
the gateway without ever importing ``elspeth_llm_gateway`` into this test
session: each subprocess starts fresh with ``cwd=gateway/``, so gateway's own
``pyproject.toml`` ``[tool.pytest.ini_options]`` pins the rootdir there and
the root repo's ``addopts``/``pythonpath`` never leak in. Imports inside each
subprocess work because ``gateway/tests/conftest.py`` and
``gateway/conformance/conftest.py`` each carry their own ``sys.path`` shim
(see those files) -- the subprocess inherits nothing from this package's own
``conftest.py`` shim above, which exists only for the SDK-isolation test
below (an in-process import, not a subprocess).
"""

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATEWAY_ROOT = _REPO_ROOT / "gateway"


def _run_gateway_pytest(target: Path) -> subprocess.CompletedProcess:
    # -p no:xdist: this repo's root pyproject.toml registers an
    # `elspeth-xdist-auto` pytest11 entry point (installed into this same
    # shared venv), so a bare subprocess pytest invocation here would pick
    # it up too -- nesting an xdist worker pool inside whatever worker pool
    # is already running the outer `pytest tests/`. Disabling it in the
    # subprocess avoids that nesting; it has no bearing on the outer run.
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(target), "-q", "-p", "no:xdist"],
        cwd=str(_GATEWAY_ROOT),
        capture_output=True,
        text=True,
    )


def _assert_clean_run(result: subprocess.CompletedProcess) -> None:
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
    assert result.returncode == 0


def test_gateway_conformance_suite_passes_as_a_subprocess():
    """The conformance kit itself: ``gateway/conformance`` run exactly as an
    agency would run it, standalone against the mock stack it builds
    in-process (no ``GATEWAY_CONFORMANCE_URL`` set)."""
    result = _run_gateway_pytest(_GATEWAY_ROOT / "conformance")
    _assert_clean_run(result)


def test_gateway_unit_suite_passes_as_a_subprocess():
    """The gateway's own unit/integration suite: ``gateway/tests``."""
    result = _run_gateway_pytest(_GATEWAY_ROOT / "tests")
    _assert_clean_run(result)


def test_sdk_module_import_does_not_pull_in_core():
    """``elspeth_llm_gateway.sdk`` must not import ``elspeth_llm_gateway.core``.

    The SDK is the adapter-facing surface a third-party adapter package
    depends on; it must stay importable without dragging in the FastAPI
    app, the config loader, or any of core's machinery (deferred minor from
    Task 2). Checked in a fresh subprocess -- never against this process's
    own ``sys.modules``, which may already carry ``core`` from an earlier,
    unrelated import in this same test session.
    """
    probe = (
        "import sys\n"
        "import elspeth_llm_gateway.sdk\n"
        "leaked = sorted(name for name in sys.modules if name.startswith('elspeth_llm_gateway.core'))\n"
        "print('LEAKED:' + ','.join(leaked))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(_GATEWAY_ROOT / "src"))
    result = subprocess.run([sys.executable, "-c", probe], cwd=str(_GATEWAY_ROOT), capture_output=True, text=True, env=env)

    assert result.returncode == 0, result.stderr

    leaked_lines = [line for line in result.stdout.splitlines() if line.startswith("LEAKED:")]
    assert leaked_lines, f"probe produced no LEAKED: line; stdout={result.stdout!r} stderr={result.stderr!r}"
    leaked = leaked_lines[0][len("LEAKED:") :]
    assert leaked == "", f"elspeth_llm_gateway.sdk import pulled in core modules: {leaked}"
