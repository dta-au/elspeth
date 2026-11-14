"""MCP server for ELSPETH pipeline composition.

Exposes the same pipeline-building tools as the web composer,
without Landscape access. Uses CatalogService for plugin discovery
and CompositionState for state mutation/validation.
"""


def create_server(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Create a composer MCP server instance. Requires the [mcp] extra."""
    from elspeth.composer_mcp.server import create_server as _create_server

    return _create_server(*args, **kwargs)


def main() -> None:
    """Process-owning console entry; execute the dedicated stdlib supervisor.

    This entry requires a fresh console interpreter. Embedded hosts construct
    create_server instead, retaining their own process and executor lifecycle.
    """
    import runpy
    import sys
    from pathlib import Path

    directory = Path(__file__).resolve().parent
    supervisor = str(directory / "portable_supervisor.py")
    sys.argv = [supervisor, sys.executable, str(directory / "runner.py"), *sys.argv[1:]]
    # A Windows CRT exec overlay can change the observed process lifetime.
    # Execute the same file in this already-fresh console process on every OS.
    runpy.run_path(supervisor, run_name="__main__")


__all__ = ["create_server", "main"]
