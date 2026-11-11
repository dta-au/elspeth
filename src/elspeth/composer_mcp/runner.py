"""Private executable: authenticate before loading the Composer runtime."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from elspeth.composer_mcp.portable_bootstrap import AuthenticatedSupervisor


def _authenticate_supervisor() -> AuthenticatedSupervisor:
    # Loading this one stdlib-only file avoids executing contracts' eager
    # application re-exports before authentication. Runtime imports subsequently
    # use this same canonical module and the same nominal wire classes.
    name = "elspeth.contracts.process_watchdog_wire"
    if name in sys.modules:
        raise RuntimeError("Private Composer runner requires a fresh interpreter")
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "contracts/process_watchdog_wire.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Composer wire contract is unavailable")
    wire = importlib.util.module_from_spec(spec)
    sys.modules[name] = wire
    spec.loader.exec_module(wire)

    from elspeth.composer_mcp.portable_bootstrap import connect_supervisor

    return connect_supervisor()


def main() -> None:
    connection = _authenticate_supervisor()

    import threading

    from elspeth.composer_mcp.portable_control import PortableProcessWatchdog
    from elspeth.contracts.process_watchdog_wire import BootstrapCompletionWitness, RecoveryReason

    draining = threading.Event()
    watchdog = PortableProcessWatchdog(connection, draining)
    try:
        from elspeth.composer_mcp.server import _parse_arguments

        args = _parse_arguments()
    except SystemExit:
        watchdog.complete_bootstrap(BootstrapCompletionWitness(watchdog.target))
        raise
    except BaseException:
        watchdog.abort_bootstrap(RecoveryReason.FAILED_STARTUP)
        raise

    import asyncio

    from elspeth.composer_mcp.standalone import run_standalone
    from elspeth.core.logging import install_asyncio_diagnostic_scrubber

    install_asyncio_diagnostic_scrubber()
    asyncio.run(run_standalone(watchdog, draining, args.scratch_dir, args.data_dir))


if __name__ == "__main__":
    main()
