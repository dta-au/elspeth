"""One-shot review-reference repair preserves candidate custody and exact IDs."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback, _FinalizerOwnedRefs
from elspeth.web.composer.state import CandidateReviewReferenceFacts
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.tools.sessions import build_set_pipeline_candidate
from tests.unit.web.composer.test_authoring_reconciliation import _exact_arguments, _node, _state, _trained_context
from tests.unit.web.composer.test_pipeline_planner import _candidate_context, _pipeline, _plan, _response, _ScriptedCompletion
from tests.unit.web.composer.test_review_reference_repair import _review_candidate


def _rejection() -> ToolResult:
    previous = replace(_state(nodes=(_node(plugin="passthrough"),)), nodes=())
    arguments = deep_thaw(_exact_arguments(previous).data)
    arguments["nodes"] = [_review_candidate(" \tService Type / PCS-LH! \n", "service_type:classify_pcs_lh")]
    return build_set_pipeline_candidate(arguments, previous, _trained_context()).result


@pytest.mark.parametrize(
    ("component", "config", "routing", "disclosed"),
    [
        ("node:classify_pcs_lh", frozenset(), frozenset(), True),
        ("node:classify_pcs_lh", frozenset({"output:other"}), frozenset(), True),
        ("node:classify_pcs_lh", frozenset({"node:classify_pcs_lh"}), frozenset(), False),
        (None, frozenset({"output:other"}), frozenset(), False),
        ("node:classify_pcs_lh", frozenset(), frozenset({"node:classify_pcs_lh"}), True),
    ],
)
def test_review_reference_feedback_obeys_per_entry_option_custody(
    component: str | None, config: frozenset[str], routing: frozenset[str], disclosed: bool
) -> None:
    result = _rejection()
    (entry,) = result.validation.errors
    # A private finalizer replacement must never cross the option boundary.
    facts = CandidateReviewReferenceFacts("PRIVATE-UNKNOWN-CANARY", ("PRIVATE-VALID-CANARY",))
    entry = replace(entry, rejected_component=component, review_reference=facts)
    result = replace(result, validation=replace(result.validation, errors=(entry,)))
    feedback = _allowlisted_candidate_feedback(result, finalizer_owned=_FinalizerOwnedRefs(config=config, routing=routing))
    (projected,) = feedback["validation"]["errors"]
    assert "detail" not in projected
    if disclosed:
        assert projected["review_reference"] == {
            "unknown_requirement_id": "PRIVATE-UNKNOWN-CANARY",
            "valid_requirement_ids": ["PRIVATE-VALID-CANARY"],
        }
    else:
        assert "review_reference" not in projected
        assert "PRIVATE-" not in json.dumps(feedback)


def test_generic_reconciliation_prose_cannot_mint_reference_facts() -> None:
    result = _rejection()
    (entry,) = result.validation.errors
    entry = replace(
        entry, review_reference=None, message="unknown interpretation requirement RAW-PRIVATE-CANARY; valid IDs RAW-PRIVATE-CANARY"
    )
    result = replace(result, validation=replace(result.validation, errors=(entry,)))
    feedback = _allowlisted_candidate_feedback(result)
    (projected,) = feedback["validation"]["errors"]
    assert "review_reference" not in projected
    assert "detail" not in projected
    assert "RAW-PRIVATE-CANARY" not in json.dumps(feedback)


def test_corrupt_owned_reference_carrier_fails_loudly() -> None:
    result = _rejection()
    (entry,) = result.validation.errors
    entry = replace(entry, review_reference=object())
    result = replace(result, validation=replace(result.validation, errors=(entry,)))
    with pytest.raises(AttributeError, match="unknown_requirement_id"):
        _allowlisted_candidate_feedback(result)


def test_reference_inventory_keeps_empty_list_and_is_closed_to_other_codes() -> None:
    result = _rejection()
    (entry,) = result.validation.errors
    entry = replace(entry, review_reference=CandidateReviewReferenceFacts("missing", ()))
    result = replace(result, validation=replace(result.validation, errors=(entry,)))
    (projected,) = _allowlisted_candidate_feedback(result)["validation"]["errors"]
    assert projected["review_reference"] == {"unknown_requirement_id": "missing", "valid_requirement_ids": []}
    entry = replace(entry, error_code="plugin_options_invalid")
    result = replace(result, validation=replace(result.validation, errors=(entry,)))
    (projected,) = _allowlisted_candidate_feedback(result)["validation"]["errors"]
    assert "review_reference" not in projected


@pytest.mark.asyncio
@pytest.mark.parametrize("routing_owned", [False, True])
async def test_one_shot_provider_corrects_unknown_reference_from_actual_feedback(tmp_path: Path, routing_owned: bool) -> None:
    previous = replace(_state(nodes=(_node(plugin="passthrough"),)), nodes=())
    candidate = _pipeline(tmp_path)
    candidate["source"]["on_success"] = "in"
    candidate["source"]["options"]["schema"] = {"mode": "fixed", "fields": ["case_study: str"]}
    candidate["outputs"][0]["sink_name"] = "out"
    candidate["nodes"] = [_review_candidate(" \tService Type / PCS-LH! \n", "service_type:classify_pcs_lh")]
    candidate["nodes"][0]["options"]["system_prompt"] = "Classify the supplied case study using the reviewed definition."

    class RepairingProvider(_ScriptedCompletion):
        async def __call__(self, **kwargs: Any) -> Any:
            self.requests.append(deepcopy(kwargs))
            if len(self.requests) == 1:
                return _response(("emit_pipeline_proposal", {"pipeline": candidate}))
            assert len(self.requests) == 2
            feedback = json.loads(kwargs["messages"][-1]["content"])
            (error,) = feedback["validation"]["errors"]
            assert error["error_code"] == "review_reconciliation_failed"
            assert "APPROVED" not in error["explanation"]
            assert "detail" not in error
            reference = error["review_reference"]
            assert reference["unknown_requirement_id"] == "service_type:classify_pcs_lh"
            expected = "Service Type / PCS-LH!:classify_pcs_lh"
            assert expected in reference["valid_requirement_ids"]
            repaired = deepcopy(candidate)
            repaired["nodes"][0]["options"]["prompt_template_parts"][1]["requirement_id"] = next(
                item for item in reference["valid_requirement_ids"] if item == expected
            )
            return _response(("emit_pipeline_proposal", {"pipeline": repaired}))

    context, _, _ = _candidate_context(tmp_path, "review-repair")
    completion = RepairingProvider()
    recorder = BufferingRecorder()

    def finalize(authored: dict[str, Any]) -> dict[str, Any]:
        finalized = deepcopy(authored)
        if routing_owned:
            finalized["nodes"][0]["on_error"] = "out"
        return finalized

    proposal = await _plan(
        tmp_path=tmp_path,
        tool_context=context,
        completion=completion,
        current_state=previous,
        repair_budget=1,
        model_overrides={"escape_hatch_model": None},
        candidate_finalizer=finalize,
        recorder=recorder,
    )
    assert len(completion.requests) == 2
    assert [candidate.status for candidate in recorder.private_candidates] == ["rejected", "accepted"]
    assert len({candidate.evidence_id for candidate in recorder.private_candidates}) == 2
    assert "service_type:classify_pcs_lh" in recorder.private_candidates[0].arguments_canonical
    assert "Service Type / PCS-LH!:classify_pcs_lh" in recorder.private_candidates[1].arguments_canonical
    assert recorder.private_candidates[0].result_canonical is not None
    assert recorder.private_candidates[1].result_canonical is None
    assert proposal.proposal.repair_count == 1
    (node,) = deep_thaw(proposal.proposal.pipeline)["nodes"]
    assert node["id"] == "classify_pcs_lh"
    assert node["options"]["prompt_template_parts"][1]["requirement_id"] == "Service Type / PCS-LH!:classify_pcs_lh"
