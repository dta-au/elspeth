"""Verify/stage supplied release bytes or explicitly install inactive fixed files.

Actual installation requires an independently authenticated key-free operator
installer. Services, accounts, credential enrollment and qualification are
separate actions. Source tests use only verify and project-owned fixture stage.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from elspeth_lints.core.runtime_artifacts import ArtifactError, install_release, stage_release, verify_release


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "stage", "install"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--release", type=Path, required=True)
        subparser.add_argument("--expected-sha256", required=True)
        if name == "stage":
            subparser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            verify_release(args.release, expected_sha256=args.expected_sha256)
        elif args.command == "stage":
            stage_release(args.release, expected_sha256=args.expected_sha256, root=args.root)
        else:
            install_release(args.release, expected_sha256=args.expected_sha256)
    except (ArtifactError, OSError, RuntimeError):
        sys.stderr.write("runtime release operation refused; services and credentials were not activated.\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
