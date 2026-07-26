"""Sealed read-only MCP tools for the Codex CLI judge transport.

This server is intentionally separate from the broader ``elspeth-judge`` MCP
surface.  A Codex judge receives exactly three source-inspection tools and no
staging, signing, shell, write, or network capability.  Every path is checked
through :class:`AgentToolScope`; content reads additionally pass through the
shared source-secret scrubber before any bytes are returned.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from elspeth_lints.core.judge import AgentToolScope, _tool_scope_decision
from elspeth_lints.core.source_excerpt import scrub_secrets

_MAX_READ_LINES = 400
_MAX_RESULT_CHARS = 50_000
_MAX_FILE_RESULTS = 500
_MAX_SCANNED_FILES = 20_000
_SENSITIVE_ENV_NAMES = frozenset(
    {
        "ELSPETH_JUDGE_METADATA_HMAC_KEY",
        "ELSPETH_JUDGE_OVERRIDE_TOKEN",
        "ELSPETH_JUDGE_OVERRIDE_TOKEN_SHA256",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    }
)


@dataclass(slots=True)
class _Context:
    scope: AgentToolScope
    calls: int = 0

    def admit_call(self) -> None:
        self.calls += 1
        if self.calls > self.scope.max_turns:
            raise ValueError(f"read-only judge tool budget exhausted (max_calls={self.scope.max_turns})")


def _assert_keyless_environment() -> None:
    present = sorted(name for name in _SENSITIVE_ENV_NAMES if os.environ.get(name))
    if present:
        raise RuntimeError(f"Codex judge tool server refuses to start with sensitive operator credentials in its environment: {present}")


def _required_str(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name!r} must be a non-empty string")
    return value


def _optional_path(arguments: dict[str, Any]) -> str:
    value = arguments.get("path")
    if value is None:
        return "."
    if not isinstance(value, str) or not value:
        raise ValueError("'path' must be a non-empty string when supplied")
    return value


def _resolve(scope: AgentToolScope, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = scope.cwd / path
    return Path(os.path.realpath(path))


def _guard(scope: AgentToolScope, tool: str, arguments: dict[str, Any]) -> None:
    allowed, reason = _tool_scope_decision(scope, tool, arguments)
    if not allowed:
        raise ValueError(reason)


def _safe_text(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    scrubbed = scrub_secrets(raw, path_hint=str(path))
    if scrubbed.redactions:
        patterns = sorted({record.pattern_name for record in scrubbed.redactions})
        raise ValueError(f"read denied: {path} contains source bytes matched by the secret scrubber ({patterns})")
    return raw


def _read_file(scope: AgentToolScope, arguments: dict[str, Any]) -> str:
    raw_path = _required_str(arguments, "file_path")
    _guard(scope, "Read", {"file_path": raw_path})
    path = _resolve(scope, raw_path)
    start_line = arguments.get("start_line", 1)
    line_count = arguments.get("line_count", 200)
    if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line <= 0:
        raise ValueError("'start_line' must be a positive integer")
    if not isinstance(line_count, int) or isinstance(line_count, bool) or not 1 <= line_count <= _MAX_READ_LINES:
        raise ValueError(f"'line_count' must be between 1 and {_MAX_READ_LINES}")
    lines = _safe_text(path).splitlines()
    selected = lines[start_line - 1 : start_line - 1 + line_count]
    rendered = "\n".join(f"{number}: {line}" for number, line in enumerate(selected, start=start_line))
    return rendered[:_MAX_RESULT_CHARS]


def _validate_glob_pattern(pattern: str) -> None:
    pure = PurePath(pattern)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError("glob pattern must be relative and may not contain '..'")


def _glob_files(scope: AgentToolScope, arguments: dict[str, Any]) -> str:
    pattern = _required_str(arguments, "pattern")
    _validate_glob_pattern(pattern)
    raw_base = _optional_path(arguments)
    _guard(scope, "Glob", {"path": raw_base, "pattern": pattern})
    base = _resolve(scope, raw_base)
    matches: list[str] = []
    for candidate in base.glob(pattern):
        resolved = Path(os.path.realpath(candidate))
        allowed, _reason = _tool_scope_decision(
            scope,
            "Glob",
            {"path": str(resolved), "pattern": pattern},
        )
        if not allowed or not resolved.is_file():
            continue
        matches.append(str(resolved))
        if len(matches) >= _MAX_FILE_RESULTS:
            break
    return json.dumps({"files": sorted(matches), "truncated": len(matches) >= _MAX_FILE_RESULTS})


def _iter_files(base: Path, file_glob: str) -> Any:
    if base.is_file():
        yield base
        return
    yield from base.glob(file_glob)


def _grep_files(scope: AgentToolScope, arguments: dict[str, Any]) -> str:
    pattern = _required_str(arguments, "pattern")
    raw_base = _optional_path(arguments)
    output_mode = arguments.get("output_mode")
    if output_mode not in {"files_with_matches", "count"}:
        raise ValueError("'output_mode' must be 'files_with_matches' or 'count'")
    file_glob = arguments.get("glob", "**/*")
    if not isinstance(file_glob, str) or not file_glob:
        raise ValueError("'glob' must be a non-empty string")
    _validate_glob_pattern(file_glob)
    _guard(
        scope,
        "Grep",
        {"path": raw_base, "pattern": pattern, "output_mode": output_mode},
    )
    base = _resolve(scope, raw_base)
    matched_files: list[str] = []
    match_count = 0
    scanned = 0
    for candidate in _iter_files(base, file_glob):
        if scanned >= _MAX_SCANNED_FILES:
            break
        resolved = Path(os.path.realpath(candidate))
        if not resolved.is_file():
            continue
        scanned += 1
        # Reuse the stronger Read guard for every searched file.  This prevents
        # adaptive Grep count queries from becoming an oracle over files the
        # shared secret scrubber would redact.
        allowed, _reason = _tool_scope_decision(scope, "Read", {"file_path": str(resolved)})
        if not allowed:
            continue
        try:
            text = _safe_text(resolved)
        except ValueError:
            continue
        count = text.count(pattern)
        if count == 0:
            continue
        match_count += count
        matched_files.append(str(resolved))
        if len(matched_files) >= _MAX_FILE_RESULTS:
            break
    payload: dict[str, Any] = {
        "scanned_files": scanned,
        "truncated": scanned >= _MAX_SCANNED_FILES or len(matched_files) >= _MAX_FILE_RESULTS,
    }
    if output_mode == "count":
        payload["count"] = match_count
    else:
        payload["files"] = sorted(matched_files)
    return json.dumps(payload, sort_keys=True)


def create_server(scope: AgentToolScope) -> Any:
    """Create the three-tool MCP server bound to ``scope``."""
    from mcp.server import Server
    from mcp.types import CallToolResult, TextContent, Tool

    ctx = _Context(scope=scope)
    server = Server("elspeth-judge-readonly")
    tools = {
        "read_file": Tool(
            name="read_file",
            description="Read a bounded line range from one permitted, secret-scrubbed source file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "line_count": {"type": "integer", "minimum": 1, "maximum": _MAX_READ_LINES},
                },
                "required": ["file_path"],
            },
        ),
        "grep_files": Tool(
            name="grep_files",
            description=(
                "Search secret-scrubbed permitted files for a literal string; "
                "returns only file names or an aggregate count, never matching content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {"type": "string"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["files_with_matches", "count"],
                    },
                },
                "required": ["pattern", "output_mode"],
            },
        ),
        "glob_files": Tool(
            name="glob_files",
            description="List files matching a relative glob under a permitted root.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        ),
    }

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_tools() -> list[Tool]:
        return list(tools.values())

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult | list[TextContent]:
        try:
            ctx.admit_call()
            if name == "read_file":
                text = _read_file(scope, arguments)
            elif name == "grep_files":
                text = _grep_files(scope, arguments)
            elif name == "glob_files":
                text = _glob_files(scope, arguments)
            else:
                raise ValueError(f"unknown tool {name!r}")
        except (OSError, ValueError) as exc:
            return CallToolResult(
                content=[TextContent(type="text", text=str(exc))],
                isError=True,
            )
        return [TextContent(type="text", text=text)]

    return server


async def run_server(scope: AgentToolScope) -> None:
    from mcp.server.stdio import stdio_server

    server = create_server(scope)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="elspeth-codex-judge-tools",
        description="Sealed read-only source tools for the Codex judge transport",
    )
    parser.add_argument("--allowed-root", action="append", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, required=True)
    args = parser.parse_args(argv)
    _assert_keyless_environment()
    roots = tuple(Path(os.path.realpath(root)) for root in args.allowed_root)
    scope = AgentToolScope(
        allowed_roots=roots,
        cwd=Path(os.path.realpath(args.cwd)),
        max_turns=args.max_calls,
    )
    asyncio.run(run_server(scope))


if __name__ == "__main__":  # pragma: no cover - exercised by Codex subprocess
    main()
