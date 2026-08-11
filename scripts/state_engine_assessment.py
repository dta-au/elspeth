#!/usr/bin/env python3
"""Create and validate reproducible state-engine proof packages."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIRECTORY.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.state_engine_assessment_lib.common import load_unique_json  # noqa: E402
from scripts.state_engine_assessment_lib.operations import main  # noqa: E402
from scripts.state_engine_assessment_lib.package import initialize_full  # noqa: E402

__all__ = ["initialize_full", "load_unique_json", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
