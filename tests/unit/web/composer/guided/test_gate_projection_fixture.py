"""F11 two-sided drift guard for the gate proposal projection fixture.

The gate-behavior wire contract is hand-mirrored in three places: the backend
key set (``web/composer/guided/protocol.py`` via
``build_guided_proposal_projection``) and the two frontend exact-key lists in
``guidedDecoder.ts``. The frontend side is guarded by
``guidedDecoder.gate.test.ts``, which decodes the checked-in fixture

    src/elspeth/web/frontend/src/api/__fixtures__/gateProposalProjection.json

as REAL backend output. This test is the backend side of the same guard: it
rebuilds the projection through ``build_guided_proposal_projection`` — the
exact production code path — with the same deterministic inputs the fixture
was generated from, and asserts byte-level JSON equality with the checked-in
fixture. If the backend contract moves, this test goes red here; if only the
fixture is edited by hand, this test goes red here; if the frontend decoder
drifts, the vitest suite goes red there. Regenerate the fixture by updating
the expected payload from this test's inputs (json.dumps(payload, indent=2,
sort_keys=True) + newline) and re-running BOTH suites.
"""

from __future__ import annotations

import json
from dataclasses import replace
from itertools import count
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import elspeth.web
import elspeth.web.composer.guided.planning as guided_planning
from elspeth.core.canonical import stable_hash
from elspeth.web.composer.guided.planning import (
    build_guided_proposal_projection,
    guided_private_reviewed_facts,
    verify_guided_proposal_projection,
)
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.composer.pipeline_proposal import PipelineProposal, PlannerSurface, PresentBase

_SOURCE_ID = "00000000-0000-4000-8000-000000000301"
_OUTPUT_ID = "00000000-0000-4000-8000-000000000302"
_PROPOSAL_ID = UUID("00000000-0000-4000-8000-000000000303")
_CHECKPOINT_ID = UUID("00000000-0000-4000-8000-000000000304")
_GATE_ID = "00000000-0000-4000-8000-000000000305"

_FIXTURE_PATH = Path(elspeth.web.__file__).parent / "frontend" / "src" / "api" / "__fixtures__" / "gateProposalProjection.json"

_CATALOG_PLUGIN_IDS = {
    "source": frozenset({"csv"}),
    "transform": frozenset({"passthrough"}),
    "sink": frozenset({"json"}),
}


def _guided() -> GuidedSession:
    return replace(
        GuidedSession.initial(),
        reviewed_sources={
            _SOURCE_ID: SourceResolved(
                name="primary",
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                observed_columns=("name", "amount"),
                sample_rows=({"name": "fixture", "amount": 42},),
                on_validation_failure="discard",
            )
        },
        reviewed_outputs={
            _OUTPUT_ID: SinkOutputResolved(
                name="cleaned",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                required_fields=("name",),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
        source_order=(_SOURCE_ID,),
        output_order=(_OUTPUT_ID,),
        step=GuidedStep.STEP_3_TRANSFORMS,
    )


def _proposal(guided: GuidedSession) -> PipelineProposal:
    return PipelineProposal.create(
        pipeline={
            "sources": {
                "primary": {
                    "plugin": "csv",
                    "on_success": "gate-input",
                    "options": {"schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [
                {
                    "id": _GATE_ID,
                    "node_type": "gate",
                    "plugin": None,
                    "input": "gate-input",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "condition": "row['amount'] > 500",
                    "routes": {"true": "accepted", "false": "cleaned"},
                    "fork_to": [],
                },
                {
                    "id": "copy",
                    "node_type": "transform",
                    "plugin": "passthrough",
                    "input": "accepted",
                    "on_success": "cleaned",
                    "on_error": "discard",
                    "options": {},
                },
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "cleaned",
                    "plugin": "json",
                    "options": {"schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                }
            ],
        },
        base=PresentBase(state_id=_CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )


def test_gate_projection_matches_the_checked_in_frontend_fixture() -> None:
    """Backend projection output must equal the fixture the frontend decodes."""
    assert _FIXTURE_PATH.is_file(), f"drift-guard fixture missing: {_FIXTURE_PATH}"

    guided = _guided()
    proposal = _proposal(guided)
    allocated = count(400)

    def fixed_uuid4() -> UUID:
        return UUID(f"00000000-0000-4000-8000-{next(allocated):012d}")

    with patch.object(guided_planning, "uuid4", fixed_uuid4):
        payload = build_guided_proposal_projection(
            proposal_id=_PROPOSAL_ID,
            proposal=proposal,
            guided=guided,
            catalog_plugin_ids=_CATALOG_PLUGIN_IDS,
        )
    # The backend's own verifier must bless what the fixture claims is real
    # backend output — equality with a payload the verifier rejects would
    # prove nothing.
    verify_guided_proposal_projection(
        payload=payload,
        proposal_id=_PROPOSAL_ID,
        proposal=proposal,
        guided=guided,
        catalog_plugin_ids=_CATALOG_PLUGIN_IDS,
    )

    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    # Round-trip through JSON so tuples/lists and other JSON-equivalent
    # container types compare by wire shape, exactly as the frontend sees it.
    assert json.loads(json.dumps(payload)) == fixture

    # Canary details the frontend suite also depends on, asserted here so a
    # red run names the drift instead of dumping two large dicts.
    gate = next(node for node in fixture["nodes"] if node["node_type"] == "gate")
    assert gate["behavior"]["condition"] == "row['amount'] > 500"
    assert [binding["key"] for binding in gate["behavior"]["routes"]] == ["false", "true"]
