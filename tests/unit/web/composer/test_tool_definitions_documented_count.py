"""DOC-001 (#327): the documented tool count is measured from the live registry.

``get_tool_definitions`` documents how many definitions it returns and how
they split by kind. That prose is a claim; the registry is the measurement.
Every number in the documented breakdown is compared with a count derived
from ``_REGISTERED_TOOLS`` and the emitted definition list, so a wrong
subtotal fails as surely as a wrong total.
"""

from __future__ import annotations

import inspect
import re
from collections import Counter

from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools._registry import _REGISTERED_TOOLS, _TOOL_DEFS_BY_NAME
from elspeth.web.composer.tools.declarations import ToolKind

_DOCUMENTED_BREAKDOWN = re.compile(
    r"Returns (?P<total>\d+) tools: (?P<discovery>\d+) discovery \+ (?P<mutation>\d+) mutation \+ "
    r"(?P<blob>\d+) blob tools \((?P<blob_discovery>\d+) discovery,\s+(?P<blob_mutation>\d+) mutation\) \+ "
    r"(?P<secret>\d+) secret tools \((?P<secret_discovery>\d+) discovery, (?P<secret_mutation>\d+) mutation\) \+ "
    r"(?P<advisor>\d+) advisor tool \+\s+(?P<interpretation>\d+) session-aware interpretation-review tool\."
)


def _documented_counts() -> dict[str, int]:
    docstring = inspect.getdoc(get_tool_definitions)
    assert docstring is not None
    match = _DOCUMENTED_BREAKDOWN.search(docstring)
    # A reworded docstring must not silently turn this into a vacuous pass.
    assert match is not None, "get_tool_definitions no longer documents its count in the pinned breakdown form"
    return {key: int(value) for key, value in match.groupdict().items()}


def _measured_counts() -> dict[str, int]:
    kinds = Counter(declaration.kind for declaration in _REGISTERED_TOOLS)
    emitted_names = [definition["name"] for definition in get_tool_definitions()]
    inline_names = set(emitted_names) - set(_TOOL_DEFS_BY_NAME)
    assert len(emitted_names) == len(set(emitted_names)), "duplicate tool names in the emitted definitions"
    return {
        "total": len(emitted_names),
        "discovery": kinds[ToolKind.DISCOVERY],
        "mutation": kinds[ToolKind.MUTATION],
        "blob": kinds[ToolKind.BLOB_DISCOVERY] + kinds[ToolKind.BLOB_MUTATION],
        "blob_discovery": kinds[ToolKind.BLOB_DISCOVERY],
        "blob_mutation": kinds[ToolKind.BLOB_MUTATION],
        "secret": kinds[ToolKind.SECRET_DISCOVERY] + kinds[ToolKind.SECRET_MUTATION],
        "secret_discovery": kinds[ToolKind.SECRET_DISCOVERY],
        "secret_mutation": kinds[ToolKind.SECRET_MUTATION],
        "advisor": len(inline_names & {"request_advisor_hint"}),
        "interpretation": len(inline_names & {"request_interpretation_review"}),
    }


def test_inline_definitions_are_exactly_the_two_documented_carve_outs() -> None:
    """The breakdown names two inline tools; any other inline name would be uncounted."""
    emitted_names = {definition["name"] for definition in get_tool_definitions()}
    assert emitted_names - set(_TOOL_DEFS_BY_NAME) == {"request_advisor_hint", "request_interpretation_review"}


def test_documented_breakdown_matches_the_live_registry() -> None:
    measured = _measured_counts()
    documented = _documented_counts()
    assert documented == measured


def test_measured_breakdown_sums_to_the_emitted_total() -> None:
    measured = _measured_counts()
    parts = measured["discovery"] + measured["mutation"] + measured["blob"] + measured["secret"]
    assert parts + measured["advisor"] + measured["interpretation"] == measured["total"]
