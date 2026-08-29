"""Registry↔TypeScript parity for composer tool-call descriptions.

The web dispatch registry (``_dispatch._REGISTERED_TOOLS``) is the source of
truth for which SYNCHRONOUS tools exist; ``discovery._SESSION_AWARE_TOOL_NAMES``
is the source of truth for the hand-maintained session-aware carve-out
(tools dispatched through the async compose-loop path instead of
``execute_tool`` — see that module's docstring). Together they are every
tool a user can see a ribbon for. ``toolCallDescriptions.ts`` must carry a
humanised sentence for every one of them — an unmapped name falls to the
generic "Composer tool call." and, after elspeth-af559a0bab, ships a raw
snake_case primary label (elspeth-af559a0bab live-check finding:
``request_interpretation_review`` shipped exactly that regression because it
is deliberately NOT a ``ToolDeclaration`` and so was invisible to
``_REGISTERED_TOOLS`` alone). The read-only / mutating halves must also agree
with the registry's ToolKind, because "Looked up:" is an honesty claim
(elspeth-f5e6723133): a DISCOVERY tool missing from the read-only half loses
its lookup label; a MUTATION tool added to it would fabricate a read. Every
session-aware tool is a durable interpretation-event write, so it belongs in
the mutating half — never the read-only one.

The TS map may carry MORE names than the web registry (MCP-only session
tools such as ``generate_yaml`` and ``load_session``); the subset assertions
below are deliberate.
"""

from __future__ import annotations

import re
from pathlib import Path

import elspeth
from elspeth.web.composer.tools._dispatch import _REGISTERED_TOOLS
from elspeth.web.composer.tools.discovery import _SESSION_AWARE_TOOL_NAMES

_PACKAGE_ROOT = Path(elspeth.__file__).parent
_TS_PATH = _PACKAGE_ROOT / "web" / "frontend" / "src" / "components" / "chat" / "toolCallDescriptions.ts"

_READ_ONLY_BLOCK_RE = re.compile(r"const READ_ONLY_TOOL_CALL_DESCRIPTIONS[^=]*=\s*\{(.*?)\n\};", re.DOTALL)
_MUTATING_BLOCK_RE = re.compile(r"const MUTATING_TOOL_CALL_DESCRIPTIONS[^=]*=\s*\{(.*?)\n\};", re.DOTALL)
# Prettier-stable record form: two-space indent, bare snake_case key, colon.
_KEY_RE = re.compile(r"^\s{2}([a-z][a-z0-9_]*):", re.MULTILINE)

# Post-change sizes (14 + 7 read-only, 17 + 8 mutating). Pinned as floors so a
# half silently emptied by a bad edit fails here even if the subset tests
# above still pass for the names that remain.
_MIN_READ_ONLY = 21
_MIN_MUTATING = 26


def _ts_halves() -> tuple[set[str], set[str]]:
    text = _TS_PATH.read_text(encoding="utf-8")
    read_only = _READ_ONLY_BLOCK_RE.search(text)
    mutating = _MUTATING_BLOCK_RE.search(text)
    assert read_only is not None, f"READ_ONLY map literal not found in {_TS_PATH.name}"
    assert mutating is not None, f"MUTATING map literal not found in {_TS_PATH.name}"
    return set(_KEY_RE.findall(read_only.group(1))), set(_KEY_RE.findall(mutating.group(1)))


def test_every_registered_tool_has_a_description() -> None:
    read_only, mutating = _ts_halves()
    mapped = read_only | mutating
    registered = {tool.name for tool in _REGISTERED_TOOLS}
    missing = registered - mapped
    assert not missing, (
        f"Registered tools with no toolCallDescriptions.ts entry: {sorted(missing)}. "
        "Add an audience-facing sentence to the correct half (kind-matched, see below)."
    )


def test_registry_kind_agrees_with_the_read_only_split() -> None:
    read_only, mutating = _ts_halves()
    for tool in _REGISTERED_TOOLS:
        if tool.kind.name.endswith("DISCOVERY"):
            assert tool.name in read_only, (
                f"{tool.name} is {tool.kind.name} but not in the read-only half — it would lose its honest 'Looked up' label."
            )
        else:
            assert tool.kind.name.endswith("MUTATION"), f"unclassified ToolKind: {tool.kind}"
            assert tool.name in mutating, (
                f"{tool.name} is {tool.kind.name} but not in the mutating half — the read-only map must never absorb a durable write."
            )


def test_ts_halves_keep_their_post_change_size() -> None:
    read_only, mutating = _ts_halves()
    assert len(read_only) >= _MIN_READ_ONLY and len(mutating) >= _MIN_MUTATING, (
        f"Too few keys matched ({len(read_only)}/{len(mutating)}) — a half was emptied, or the record shape / regex has drifted."
    )


def test_every_session_aware_tool_has_a_mutating_description() -> None:
    # elspeth-af559a0bab live-check finding: request_interpretation_review is
    # deliberately NOT a ToolDeclaration (see discovery.py), so it is invisible
    # to _REGISTERED_TOOLS and the two tests above never see it. It is a
    # durable interpretation-event write dispatched through the session-aware
    # path, so it must appear in the mutating half — never the read-only one,
    # which would mislabel a write as a "Looked up" read.
    read_only, mutating = _ts_halves()
    missing = _SESSION_AWARE_TOOL_NAMES - mutating
    assert not missing, (
        f"Session-aware tools with no mutating toolCallDescriptions.ts entry: {sorted(missing)}. "
        "Add an audience-facing sentence to MUTATING_TOOL_CALL_DESCRIPTIONS."
    )
    misfiled = _SESSION_AWARE_TOOL_NAMES & read_only
    assert not misfiled, (
        f"Session-aware tools wrongly filed in the read-only half: {sorted(misfiled)}. "
        "These are durable writes and must never render 'Looked up'."
    )
