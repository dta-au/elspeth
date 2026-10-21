"""Installed only beneath the protected native launcher's fixed artifact root."""

import sys


# Native code authenticates these OS-managed paths and the complete artifact
# tree before interpreter startup. Site initialization and bytecode writes are
# disabled by native argv; neither cwd nor ambient PYTHONPATH is an import root.
def _bootstrap() -> int:
    sys.path[:] = [
        "/usr/lib/python3.12",
        "/usr/lib/python3.12/lib-dynload",
        "/opt/elspeth/operator/lib",
    ]
    from elspeth_lints.core.operator_runtime import OperatorRuntimeUnavailableError, _admit_native_runtime

    try:
        _admit_native_runtime()
    except (OperatorRuntimeUnavailableError, OSError):
        sys.stderr.write("elspeth-operator: protected runtime authentication refused; no signer action started.\n")
        return 78

    from elspeth_lints.core.cli import main

    return main()


raise SystemExit(_bootstrap())
