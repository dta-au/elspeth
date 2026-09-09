"""Every argument knob the planner is shown must reach the audit wire.

WHAT THIS GUARDS, and why it is not covered elsewhere.

A composer tool argument -- a "knob" the planner LLM can set -- is described in
several places at once, and each description is a wire that must carry it:

    SHIPPED    the json-schema property the model is shown, from the live
               ``get_tool_definitions()`` registry
    MODEL      the pydantic arguments model the handler validates against
    ADMITTED   ``MANIFEST[tool].policy.known_argument_keys`` in redaction.py
    READ       what the handler actually consumes

``tools/schema_contract.py`` already pins SHIPPED against MODEL, directionally,
for ``upsert_node`` and ``set_pipeline``. Nothing pinned SHIPPED against
ADMITTED, for any tool. That gap was not theoretical: between 2026-08-15
(80fa17fed added a ``description`` property to two tool schemas) and 2026-09-04,
``upsert_node`` and ``set_output`` advertised a knob their allowlist did not
admit, and because both run ``redact_unknown_argument_keys=True`` the audit
trail replaced the key NAME with ``<redacted-unknown-argument-key>``. The row
could not say which knob had been dropped. The schema prose meanwhile told the
model the value was "shown to reviewers on the Spec tab".

Both sides here are derived from live sources -- the registry and the manifest --
so this gate cannot itself drift out of step with what it checks. That is the
point: a gate needing a hand-edit alongside the thing it guards reproduces the
defect it is meant to catch.

SCOPE, MEASURED -- stated so a green run is not over-read. This pins the ADMITTED
wire only, and only for tools that HAVE an argument allowlist: 15 of 42 tools and
52 of 104 advertised knobs at 2026-09-04. The other 27 tools declare no allowlist
and run open, so there is nothing to compare and they are unguarded here BY
CONSTRUCTION, not by passing. A green run is silent about them.

It is also silent about READ (a knob the handler ignores), TAUGHT (skill text
naming a knob that does not exist), and the TypeScript decoder -- pytest cannot
see the frontend in this repository.

An earlier revision of this docstring claimed the gate covered "all 42 tools".
It iterated 14. That overstatement is the same defect the file exists to catch,
one level up, which is why the numbers above are measured rather than described.
"""

from __future__ import annotations

from typing import Any

from elspeth.web.composer.redaction import MANIFEST, policy_closes_unknown_arguments
from elspeth.web.composer.tools._dispatch import get_tool_definitions


def _shipped_argument_keys() -> dict[str, frozenset[str]]:
    """Advertised argument names per tool, read from the live tool registry."""
    shipped: dict[str, frozenset[str]] = {}
    for definition in get_tool_definitions():
        function: dict[str, Any] = definition.get("function", definition)
        name = function.get("name")
        if not isinstance(name, str):
            continue
        parameters = function.get("parameters") or {}
        properties = parameters.get("properties") or {}
        shipped[name] = frozenset(properties)
    return shipped


def _argument_allowlists() -> dict[str, frozenset[str]]:
    """Admitted argument names per tool, read from the live redaction manifest.

    ``MANIFEST`` maps a tool name to a ``ToolRedaction`` wrapper; the allowlist
    lives on its ``.policy``, which is ``None`` for tools carrying no policy.
    Reading ``known_argument_keys`` off the wrapper returns ``None`` for every
    tool -- a confidently wrong answer rather than an error.
    """
    admitted: dict[str, frozenset[str]] = {}
    for name, entry in MANIFEST.items():
        policy = entry.policy
        if policy is None:
            continue
        keys = policy.known_argument_keys
        if keys:
            admitted[name] = frozenset(keys)
    return admitted


def _fail_closed_tools() -> frozenset[str]:
    """Tools that redact argument keys they do not recognise.

    The predicate is IMPORTED from production, not restated here. The first
    version of this file restated it as ``redact_unknown_argument_keys`` alone;
    production's rule is ``known_argument_keys or redact_unknown_argument_keys``,
    so this gate silently skipped ``request_advisor_hint`` -- a tool whose
    unadmitted keys production really does replace with the sentinel (verified by
    sending it a bogus key). A guard that re-derives its own scope drifts from the
    authority it guards while reading as though it covered what it skipped.
    """
    return frozenset(name for name, entry in MANIFEST.items() if entry.policy is not None and policy_closes_unknown_arguments(entry.policy))


def test_every_advertised_knob_on_a_fail_closed_tool_is_admitted_by_its_allowlist() -> None:
    """A knob the model is shown must survive the audit trail intact.

    On a fail-closed tool an advertised-but-unadmitted argument is destroyed by
    name, so the audit row records that *something* unknown was sent and cannot
    say what. The model is told the knob exists; the record of it being used is
    discarded.
    """
    shipped = _shipped_argument_keys()
    admitted = _argument_allowlists()
    fail_closed = _fail_closed_tools()

    # Non-vacuity. Every accessor above can return an empty mapping without
    # raising -- `known_argument_keys` read off the ToolRedaction wrapper instead
    # of its `.policy` yields None for all 42 tools, and this assertion would
    # then iterate nothing and pass. A gate that measures nothing must fail, not
    # report success.
    assert shipped, "no tool definitions read from the live registry"
    assert admitted, "no argument allowlists read from the live redaction manifest"
    assert fail_closed, "no fail-closed tools found; the manifest accessor is reading the wrong attribute"

    unadmitted: dict[str, list[str]] = {}
    for tool in sorted(fail_closed & set(admitted)):
        missing = shipped.get(tool, frozenset()) - admitted[tool]
        if missing:
            unadmitted[tool] = sorted(missing)

    assert unadmitted == {}, (
        "These tools advertise argument keys their allowlist does not admit, and they "
        "close over unknown arguments, so the audit trail will replace each key NAME "
        f"with the unknown-argument sentinel: {unadmitted}. Add the key to "
        "known_argument_keys in redaction.py's MANIFEST, or stop advertising it."
    )


def test_no_allowlist_admits_an_argument_the_tool_does_not_advertise() -> None:
    """A dead allowlist entry is a knob that was renamed or removed elsewhere.

    This is the reverse direction and it is a staleness check, not a safety one:
    admitting a key nothing ships costs nothing at runtime, but it means the
    tuple has stopped tracking the schema and the next reader cannot tell which
    entries are load-bearing.
    """
    shipped = _shipped_argument_keys()
    admitted = _argument_allowlists()

    dead: dict[str, list[str]] = {}
    for tool in sorted(admitted):
        if tool not in shipped:
            continue
        orphaned = admitted[tool] - shipped[tool]
        if orphaned:
            dead[tool] = sorted(orphaned)

    assert dead == {}, f"Allowlist entries with no matching advertised argument: {dead}"


def test_every_tool_in_the_redaction_manifest_is_a_live_registered_tool() -> None:
    """The manifest and the registry describe the same tool set.

    A manifest entry for a tool that no longer exists is unreachable policy; a
    registered tool with no manifest entry has no redaction policy at all.
    """
    shipped = _shipped_argument_keys()

    assert sorted(set(MANIFEST) - set(shipped)) == [], "redaction manifest entries with no live registered tool"
    assert sorted(set(shipped) - set(MANIFEST)) == [], "registered tools absent from the redaction manifest"


def test_this_gates_fail_closed_filter_is_the_one_production_applies() -> None:
    """The gate's scope must equal production's, not merely resemble it.

    ``_fail_closed_tools`` calls ``policy_closes_unknown_arguments``, the same
    predicate ``_redact_via_policy`` branches on. This test fails if a future
    edit inlines the condition here again, because the two spellings that look
    equivalent are not: filtering on ``redact_unknown_argument_keys`` alone drops
    every tool that closes by declaring an allowlist without setting the flag.

    Asserted against a locally recomputed truth rather than the imported helper,
    so replacing the helper with a wrong one still fails this.
    """
    production_closed = {
        name
        for name, entry in MANIFEST.items()
        if entry.policy is not None and (entry.policy.known_argument_keys or entry.policy.redact_unknown_argument_keys)
    }

    assert _fail_closed_tools() == production_closed, (
        "This gate's fail-closed set has diverged from the rule _redact_via_policy "
        "applies. Tools it would skip: "
        f"{sorted(production_closed - _fail_closed_tools())}"
    )

    flag_only = {name for name, entry in MANIFEST.items() if entry.policy is not None and entry.policy.redact_unknown_argument_keys}
    assert production_closed - flag_only, (
        "Expected at least one tool that closes by declaring known_argument_keys "
        "without setting redact_unknown_argument_keys. If none remains, this test's "
        "premise is gone and it should be retired rather than left passing vacuously."
    )
