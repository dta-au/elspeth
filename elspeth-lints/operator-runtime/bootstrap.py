"""Installed only beneath the protected native launcher's fixed artifact root."""

import sys


# Native code authenticates these OS-managed paths and the complete artifact
# tree before interpreter startup. Site initialization and bytecode writes are
# disabled by native argv; neither cwd nor ambient PYTHONPATH is an import root.
def _fixed_import_paths(major: int, minor: int) -> list[str]:
    if type(major) is not int or type(minor) is not int or major != 3 or minor not in (12, 13):
        raise ValueError("unsupported operator Python profile")
    if minor == 12:
        return ["/usr/lib/python3.12", "/usr/lib/python3.12/lib-dynload", "/opt/elspeth/operator/lib"]
    return ["/usr/lib/python3.13", "/usr/lib/python3.13/lib-dynload", "/opt/elspeth/operator/lib"]


def _bootstrap() -> int:
    try:
        sys.path[:] = _fixed_import_paths(sys.version_info.major, sys.version_info.minor)
    except ValueError:
        sys.stderr.write("elspeth-operator: unsupported interpreter; no signer action started.\n")
        return 78
    from elspeth_lints.core.operator_runtime import OperatorRuntimeUnavailableError, _admit_native_runtime

    try:
        _admit_native_runtime()
    except (OperatorRuntimeUnavailableError, OSError):
        sys.stderr.write("elspeth-operator: protected runtime authentication refused; no signer action started.\n")
        return 78

    from elspeth_lints.core.cli import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_bootstrap())
