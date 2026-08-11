#!/usr/bin/env python3
"""Create and validate reproducible state-engine proof packages."""

from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIRECTORY.parent
SOURCE_ROOT = REPOSITORY_ROOT / "src"
CANONICAL_IMPORT_ROOTS = [str(REPOSITORY_ROOT), str(SOURCE_ROOT)]
canonical_real_paths = {os.path.realpath(path) for path in CANONICAL_IMPORT_ROOTS}
ambient_paths = [path for path in sys.path if not isinstance(path, str) or os.path.realpath(path) not in canonical_real_paths]
sys.path[:] = [*CANONICAL_IMPORT_ROOTS, *ambient_paths]

from scripts.state_engine_assessment_lib.common import load_unique_json  # noqa: E402
from scripts.state_engine_assessment_lib.operations import main  # noqa: E402
from scripts.state_engine_assessment_lib.package import initialize_full  # noqa: E402

__all__ = ["initialize_full", "load_unique_json", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
