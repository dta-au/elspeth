#!/usr/bin/env python3
"""Run ELSPETH's fail-closed, non-inert Wardline gate.

Wardline treats repository packs as executable policy. Its CLI therefore
requires both an exact pack grant and an explicit local-pack grant; declaring a
pack in ``weft.toml`` alone is intentionally insufficient. This command owns
those grants and rejects Wardline's otherwise-successful inert posture.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACK_MODULE = "scripts.wardline_pack"


class GateContractError(ValueError):
    """Wardline returned a structurally invalid or false-green summary."""


def _wardline_command(executable: str, output: Path) -> tuple[str, ...]:
    """Build the one authoritative ELSPETH Wardline invocation."""
    return (
        executable,
        "scan",
        str(REPO_ROOT),
        "--config",
        str(REPO_ROOT / "weft.toml"),
        "--format",
        "agent-summary",
        "--output",
        str(output),
        "--fail-on",
        "ERROR",
        "--trust-pack",
        PACK_MODULE,
        "--allow-custom-packs",
        "--local-only",
    )


def _validated_recognized_boundaries(payload: object) -> int:
    """Return the positive recognized-boundary count or fail closed."""
    if not isinstance(payload, Mapping) or payload.get("schema") != "wardline-agent-summary-1":
        raise GateContractError("Wardline did not return schema wardline-agent-summary-1")
    resolution = payload.get("resolution")
    if not isinstance(resolution, Mapping):
        raise GateContractError("Wardline summary is missing the resolution object")
    inert = resolution.get("inert")
    if inert is not False:
        raise GateContractError(f"Wardline resolution is inert or invalid (inert={inert!r})")
    recognized = resolution.get("recognized_boundaries")
    if isinstance(recognized, bool) or not isinstance(recognized, int):
        raise GateContractError("Wardline recognized-boundary count is not an integer")
    if recognized <= 0:
        raise GateContractError("Wardline reported zero recognized trust boundaries")
    return recognized


def _pythonpath_with_repo(env: Mapping[str, str]) -> str:
    existing = env.get("PYTHONPATH")
    return str(REPO_ROOT) + (os.pathsep + existing if existing else "")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the real scanner and enforce both its finding and activation verdicts."""
    if argv:
        print("usage: python scripts/wardline_gate.py", file=sys.stderr)
        return 2

    executable = shutil.which("wardline")
    if executable is None:
        print("wardline gate error: `wardline` is not installed or not on PATH", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["PYTHONPATH"] = _pythonpath_with_repo(env)
    with tempfile.TemporaryDirectory(prefix="elspeth-wardline-") as temp_dir:
        output = Path(temp_dir) / "summary.json"
        try:
            completed = subprocess.run(
                _wardline_command(executable, output),
                cwd=REPO_ROOT,
                env=env,
                check=False,
            )
        except OSError as exc:
            print(f"wardline gate error: failed to execute Wardline: {exc}", file=sys.stderr)
            return 2

        if completed.returncode == 2:
            return 2
        if completed.returncode not in (0, 1):
            print(f"wardline gate error: unexpected Wardline exit {completed.returncode}", file=sys.stderr)
            return 2
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"wardline gate error: unreadable agent summary: {exc}", file=sys.stderr)
            return 2
        try:
            recognized = _validated_recognized_boundaries(payload)
        except GateContractError as exc:
            print(f"wardline gate failed: {exc}", file=sys.stderr)
            return 1

    print(f"wardline activation: non-inert with {recognized} recognized trust boundaries", file=sys.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
