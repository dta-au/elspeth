#!/usr/bin/env python3
"""Legacy fingerprint-baseline command, disabled pending a protected verifier.

Baseline regeneration requires a valid signed enforce gate over the current
source and allowlist. The old command loaded the operator key, then ran mutable
checkout capture code, tests, and Git callbacks in the same key-bearing process
tree. That workflow is disabled until verification can occur across a protected
operator boundary without giving checkout maintenance signing-key custody.

The historical --env-file and --commit arguments remain recognized so existing
invocations receive an explicit refusal. No environment file is loaded, signed
gate is asserted, baseline is written, or repository callback is executed.
Shape-only verification and caller-asserted success cannot replace the signed
gate. Real signing remains disabled pending on-prem migration and qualification.
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", action="store_true", help="legacy option; regeneration is disabled.")
    parser.add_argument("--env-file", help="legacy option; no environment file will be read.")
    parser.parse_args()
    print(
        "Baseline regeneration is disabled: signed verification and checkout-controlled "
        "maintenance must not share signing-key custody. A protected verifier bound to "
        "the current source and allowlist is required before this workflow can resume. "
        "No baseline was regenerated. Do not substitute shape-only verification or an "
        "asserted gate result.",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
