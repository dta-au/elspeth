"""DOC-002 (#327): the shared result instruction names every non-envelope tool.

Most composer tools answer with the ordinary ``ToolResult`` envelope. Four
offered tools do not:

- ``request_advisor_hint`` is intercepted before ``execute_tool`` and answers
  with a flat feedback object keyed by ``status``;
- ``handoff_to_user`` and ``restore_parked_graph`` end the turn, so no tool
  result is returned to the model at all;
- the planner terminal ``emit_pipeline_proposal`` answers a rejection with
  ``success: false`` plus ``validation`` only, and an accepted proposal ends
  planning with no tool result.

The non-envelope set is derived from the live registries, not listed by
hand, so a new carve-out that the instruction does not mention turns this
red. The operator reference names all four; the shared skill names all but
the planner-exclusive terminal, which it describes without naming.
"""

from __future__ import annotations

from pathlib import Path

from elspeth.web.composer.capability_skill import PLANNER_TERMINAL_TOOL_NAME
from elspeth.web.composer.pipeline_planner import PLANNER_TERMINAL_INSTRUCTION
from elspeth.web.composer.recovery_controls import RECOVERY_TOOL_NAMES
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools._registry import _TOOL_DEFS_BY_NAME
from elspeth.web.composer.tools.discovery import _SESSION_AWARE_TOOL_NAMES

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL = REPO_ROOT / "src" / "elspeth" / "web" / "composer" / "skills" / "pipeline_composer.md"
OPERATOR_REFERENCE = REPO_ROOT / "docs" / "reference" / "composer-tools.md"


def _section(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(heading)
    end = text.index("\n## ", start + 1)
    return text[start:end]


def _non_envelope_tool_names() -> frozenset[str]:
    """Offered tools whose answer is not the ordinary ToolResult envelope.

    Inline definitions outside the declaration registry are dispatched
    outside ``execute_tool``; the session-aware ones still return a
    ``ToolResult`` and are excluded. Recovery controls and the planner
    terminal are offered by their own builders.
    """
    inline = {definition["name"] for definition in get_tool_definitions()} - set(_TOOL_DEFS_BY_NAME)
    return frozenset((inline - _SESSION_AWARE_TOOL_NAMES) | RECOVERY_TOOL_NAMES | {PLANNER_TERMINAL_TOOL_NAME})


def test_derived_non_envelope_set_is_the_audited_four() -> None:
    """Positive control: the derivation finds the four tools DOC-002 names."""
    assert _non_envelope_tool_names() == {
        "request_advisor_hint",
        "handoff_to_user",
        "restore_parked_graph",
        "emit_pipeline_proposal",
    }


def test_skill_result_section_names_every_non_envelope_tool() -> None:
    """The shared skill names each one, except the planner terminal.

    The skill is shared by the freeform loop and the proposal planner, so it
    stays surface-neutral and never names the planner-exclusive terminal
    (``test_capability_skill_identity``); that terminal's contract rides
    ``PLANNER_TERMINAL_INSTRUCTION``. The skill still describes the
    terminal's rejected and accepted forms without naming it.
    """
    section = _section(SKILL, "## Reading a tool result")
    shared = _non_envelope_tool_names() - {PLANNER_TERMINAL_TOOL_NAME}
    missing = sorted(name for name in shared if f"`{name}`" not in section)
    assert missing == [], f"'Reading a tool result' does not name: {missing}"
    assert "proposal terminal" in section
    assert PLANNER_TERMINAL_TOOL_NAME in PLANNER_TERMINAL_INSTRUCTION


def test_skill_result_section_does_not_claim_one_framing_for_every_tool() -> None:
    section = _section(SKILL, "## Reading a tool result")
    assert "Every tool returns one JSON object with the same framing" not in section


def test_skill_teaches_the_advisor_status_discriminant() -> None:
    section = _section(SKILL, "## Reading a tool result")
    assert "`status`" in section
    assert "`SUCCESS`" in section


def test_operator_reference_names_every_non_envelope_tool() -> None:
    section = _section(OPERATOR_REFERENCE, "## Tool Result Format")
    missing = sorted(name for name in _non_envelope_tool_names() if f"`{name}`" not in section)
    assert missing == [], f"operator reference 'Tool Result Format' does not name: {missing}"
    assert "Every tool returns a `ToolResult`." not in section
