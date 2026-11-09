"""Nominal operator authority for local Composer MCP paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class McpConfiguredRootAuthority:
    """Operator-issued root, independent of Web session or blob custody.

    Resolve the configured root once so admission, runtime YAML and cache
    identity share one location. Containment does not require it to exist.
    """

    root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise TypeError("MCP configured root must be a Path")
        root = self.root.resolve()
        if root.exists() and not root.is_dir():
            raise ValueError("MCP configured root must be a directory")
        object.__setattr__(self, "root", root)
