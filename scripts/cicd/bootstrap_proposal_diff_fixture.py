"""Regenerate the redacted tool-argument fixture the frontend diff tests read.

The proposal-approval surface renders ``proposal.arguments_redacted_json`` —
the POST-redaction argument payload, never the arguments the planner authored.
Frontend tests that hand-build that payload certify nothing about the producer:
that is exactly how elspeth-b1c14dd3c2 shipped four unreachable projection arms
(the consumer read ``patch`` as an object; the redactor emits a summary string).

This script closes that loop by writing one fixture file that BOTH languages
read:

* ``src/elspeth/web/frontend/src/test/fixtures/redacted-tool-arguments.json``
  is generated here by calling the real
  :func:`elspeth.web.composer.redaction.redact_tool_call_arguments`.
* The frontend vitest suites read the ``redacted`` payloads from it, so they
  are driven by producer output rather than by a hand-written guess.
* ``tests/unit/web/composer/test_proposal_diff_redaction_fixture.py``
  re-derives every ``redacted`` payload from its ``arguments`` and asserts
  equality, so a change to the redactor fails a Python test that names this
  script — the frontend cannot silently drift behind the backend again.

Idempotent: ``sort_keys=True`` plus a fixed case ordering means re-running on
an unchanged redactor produces a byte-identical file.

Usage::

    # Dry-run (print what would be written, do not modify the file):
    .venv/bin/python scripts/cicd/bootstrap_proposal_diff_fixture.py

    # Write the fixture (commit the change after reviewing the diff):
    .venv/bin/python scripts/cicd/bootstrap_proposal_diff_fixture.py --write

IN A WORKTREE, prefix that with ``PYTHONPATH=<worktree>/src``. The venv is
symlinked to the main checkout and ``elspeth`` is installed editable there, so
a bare interpreter regenerates from the MAIN checkout's redactor and writes a
file identical to the one already committed -- an empty ``git diff`` while the
test keeps failing is the symptom.

Case selection
--------------
Every case is reachable through the live tool path. Cases that pydantic
rejects before redaction runs (a non-mapping ``patch`` on ``patch_*_options``,
which raises ``ValidationError``) are deliberately absent: presenting an
unreachable shape as a live-path fixture is the same error this fixture
exists to prevent. The decoder's handling of those shapes is covered by
frontend unit tests that label themselves as defensive, not as live-path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ``__file__`` is ``<project_root>/scripts/cicd/bootstrap_proposal_diff_fixture.py``
# so parents[2] is the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

FIXTURE_PATH = PROJECT_ROOT / "src" / "elspeth" / "web" / "frontend" / "src" / "test" / "fixtures" / "redacted-tool-arguments.json"

REGENERATE_COMMAND = ".venv/bin/python scripts/cicd/bootstrap_proposal_diff_fixture.py --write"

# The composition state the frontend fixtures diff against
# (ProposalDiff.test.tsx ``makeState``). The arguments below are authored to
# line up with it: same source name, node id, sink name, and option keys.
CASES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    # --- patch_*_options: the three tools whose `patch` is summarized -------
    (
        "patch_source_options_two_scalars",
        "patch_source_options",
        {"source_name": "source", "patch": {"path": "s3://bucket/in.csv", "delimiter": ";"}},
    ),
    (
        "patch_node_options_one_mapping",
        "patch_node_options",
        {"node_id": "extract", "patch": {"mappings": {"a": "c"}}},
    ),
    (
        "patch_node_options_mixed_shapes",
        "patch_node_options",
        {
            "node_id": "extract",
            "patch": {
                "mappings": {"a": "c"},
                "tags": ["x", "y"],
                "model": "anthropic/claude-haiku-4.5",
                "unused": None,
            },
        },
    ),
    (
        "patch_node_options_empty_patch",
        "patch_node_options",
        {"node_id": "extract", "patch": {}},
    ),
    (
        "patch_node_options_missing_node",
        "patch_node_options",
        {"node_id": "ghost", "patch": {"threshold": 0.5}},
    ),
    (
        "patch_output_options_one_scalar",
        "patch_output_options",
        {"sink_name": "results", "patch": {"path": "out2.json"}},
    ),
    # --- set_metadata: the sentinel, in every reachable variant -------------
    (
        "set_metadata_name_and_description",
        "set_metadata",
        {"patch": {"name": "Renamed", "description": "Now described"}},
    ),
    ("set_metadata_name_only", "set_metadata", {"patch": {"name": "Renamed"}}),
    ("set_metadata_empty", "set_metadata", {"patch": {}}),
    (
        "set_metadata_unknown_key",
        "set_metadata",
        {"patch": {"name": "Renamed", "colour": "red"}},
    ),
    ("set_metadata_invalid", "set_metadata", {"patch": "not-a-mapping"}),
    # A patch whose ONLY key is unrecognized: `keys` comes back empty and the
    # `unknown` token stands alone, so the consumer has a proposal it can say
    # nothing specific about but must not silently render as empty.
    ("set_metadata_only_unknown_key", "set_metadata", {"patch": {"colour": "red"}}),
    # set_metadata with no arguments at all. `patch` is ABSENT from the
    # redacted payload rather than summarized, so every decoder must survive
    # `args.patch === undefined` — a case a fixture keyed only on sentinel
    # STRINGS would never surface.
    ("set_metadata_no_arguments", "set_metadata", {}),
    # --- the identity-bearing arms, which DO survive redaction -------------
    # Pinned so a future redaction change that starts summarizing `plugin` or
    # `id` fails here instead of silently emptying the proposal card.
    (
        "upsert_node_with_options",
        "upsert_node",
        {
            "id": "extract",
            "node_type": "transform",
            "plugin": "html_extract",
            "input": "rows",
            "options": {"selector": ".body"},
        },
    ),
    (
        "set_output_with_options",
        "set_output",
        {"sink_name": "errors", "plugin": "csv", "options": {"path": "errors.csv"}},
    ),
    # --- set_pipeline replaying the CURRENT state verbatim -----------------
    # The regression case for the spurious-"Changed" defect: every provided
    # key matches ``makeState``, so an honest projection reports nothing. The
    # redactor still summarizes each `options` mapping into a string, which is
    # what used to make every row differ.
    (
        "set_pipeline_replaying_current_state",
        "set_pipeline",
        {
            "source": {
                "plugin": "csv",
                "options": {"path": "input.csv"},
                "on_success": "rows",
                "on_validation_failure": "discard",
            },
            "nodes": [
                {
                    "id": "extract",
                    "node_type": "transform",
                    "plugin": "field_mapper",
                    "input": "rows",
                    "on_success": "mapped",
                    "on_error": None,
                    "options": {"mappings": {"a": "b"}},
                }
            ],
            "edges": [
                {
                    "id": "e1",
                    "from_node": "source",
                    "to_node": "extract",
                    "edge_type": "on_success",
                    "label": None,
                }
            ],
            "outputs": [{"sink_name": "results", "plugin": "json", "options": {"path": "out.json"}}],
        },
    ),
)


def build_fixture() -> dict[str, Any]:
    """Redact every case through the live producer and assemble the fixture."""
    # Imported lazily so ``--help`` works without the package importable.
    from elspeth.web.composer.redaction import redact_tool_call_arguments
    from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry

    telemetry = NoopRedactionTelemetry()
    cases: dict[str, Any] = {}
    for case_name, tool_name, arguments in CASES:
        cases[case_name] = {
            "tool": tool_name,
            "arguments": arguments,
            "redacted": redact_tool_call_arguments(tool_name, arguments, telemetry=telemetry),
        }
    return {
        "_comment": (
            "GENERATED FILE -- do not hand-edit. Each 'redacted' payload is the "
            "output of elspeth.web.composer.redaction.redact_tool_call_arguments "
            "for its 'arguments'. Frontend tests read these payloads so they are "
            "driven by the real producer; a Python test re-derives them so the "
            "two languages cannot drift (elspeth-b1c14dd3c2)."
        ),
        "_regenerate": REGENERATE_COMMAND,
        "cases": cases,
    }


def render(fixture: dict[str, Any]) -> str:
    """Canonical serialization: stable across runs, reviewable as a diff."""
    return json.dumps(fixture, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the fixture file (default: print it and leave the file alone)",
    )
    args = parser.parse_args()

    rendered = render(build_fixture())
    if not args.write:
        sys.stdout.write(rendered)
        return 0

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(rendered, encoding="utf-8")
    sys.stdout.write(f"wrote {FIXTURE_PATH.relative_to(PROJECT_ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
