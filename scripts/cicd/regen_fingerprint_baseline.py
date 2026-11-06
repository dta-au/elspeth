#!/usr/bin/env python3
"""Legacy entrypoint: use the independently installed protected operator command.

Baseline regeneration requires a valid signed enforce gate over the current
source and allowlist. The old command loaded the operator key, then ran mutable
checkout capture code, tests, and Git callbacks in the same key-bearing process
tree. The supported implementation now lives in the installed operator runtime;
this editable script must not acquire keys or act as its own trusted launcher.

The historical --env-file and --commit arguments remain recognized so existing
invocations receive an explicit refusal. No environment file is loaded, signed
gate is asserted, baseline is written, or repository callback is executed.
Run the fixed entrypoint in a qualified installation:
  /usr/local/libexec/elspeth-operator regenerate-fingerprint-baseline \\
      --repo-root /path/to/checkout --env-file /path/to/operator.env
Review and commit the resulting fixture separately in a key-free shell. The
protected command performs no Git mutation. Missing installed admission refuses;
shape-only verification and asserted success never replace the signed gate.
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", action="store_true", help="legacy option; commit separately from key custody.")
    parser.add_argument("--env-file", help="legacy option; no environment file will be read.")
    parser.parse_args()
    print(
        "Baseline regeneration requires the protected operator entrypoint: "
        "/usr/local/libexec/elspeth-operator regenerate-fingerprint-baseline "
        "--repo-root /path/to/checkout --env-file /path/to/operator.env. "
        "This editable script did not load keys, run capture or change a baseline. "
        "Commit separately in a key-free shell; do not substitute shape-only verification.",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
