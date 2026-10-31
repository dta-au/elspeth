"""Key-free offline release assembly from externally digest-bound supplied files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from elspeth_lints.core.runtime_artifacts import ArtifactError, build_release


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--expected-catalog-sha256", required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        identity = build_release(
            catalog=args.catalog, expected_sha256=args.expected_catalog_sha256, input_root=args.input_root, output=args.output
        )
    except (ArtifactError, OSError, RuntimeError):
        sys.stderr.write("offline release assembly refused; no runtime is qualified.\n")
        return 2
    sys.stdout.write(identity + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
