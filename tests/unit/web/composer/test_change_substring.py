"""Compact prompt edits preserve exact text and the option-patch guard chain."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest

from elspeth.web.composer.tools._registry import _MUTATION_TOOL_NAMES
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_prompt_patch_reviews import _approved_state
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool


def _edit(text: str, old: str, replacement: str, *, replace_all: bool = False) -> dict[str, object]:
    return {
        "target": {"kind": "node_option", "node_id": "code_themes", "option": "prompt_template"},
        "expected_sha256": sha256(text.encode("utf-8")).hexdigest(),
        "expected_state_id": "state-1",
        "edits": [{"old": old, "replacement": replacement, "replace_all": replace_all}],
    }


def test_unique_prompt_edit_reopens_review_without_echoing_text() -> None:
    assert "change_substring" in _MUTATION_TOOL_NAMES
    state = _approved_state()
    text = state.nodes[0].options["prompt_template"]
    args = _edit(text, "Classify", "Assess")
    result = execute_tool("change_substring", args, state, _mock_catalog(), current_state_id="state-1")
    assert result.success, result.validation.errors
    assert result.updated_state.nodes[0].options["prompt_template"] == text.replace("Classify", "Assess")
    assert "approved_prompt_artifact_hash" not in result.updated_state.nodes[0].options
    assert "Classify" not in repr(result.to_dict())
    assert "Assess" not in repr(result.to_dict())
    assert result.data["replacement_count"] == 1
    assert "state_id" not in result.data
    assert result.data["sha256"] == sha256(result.updated_state.nodes[0].options["prompt_template"].encode("utf-8")).hexdigest()


def test_noop_edit_keeps_the_inspected_revision_and_review() -> None:
    assert "change_substring" in _MUTATION_TOOL_NAMES
    state = _approved_state()
    text = state.nodes[0].options["prompt_template"]
    args = _edit(text, "Classify", "Classify")
    result = execute_tool("change_substring", args, state, _mock_catalog(), current_state_id="state-1")
    assert result.success
    assert result.updated_state is state
    assert result.data["changed"] is False
    assert "state_id" not in result.data


@pytest.mark.parametrize("old,replace_all,code", [("e", False, "substring_not_unique"), ("missing", False, "substring_not_found")])
def test_exact_matching_refuses_atomically(old: str, replace_all: bool, code: str) -> None:
    assert "change_substring" in _MUTATION_TOOL_NAMES
    state = _approved_state()
    text = state.nodes[0].options["prompt_template"]
    args = _edit(text, old, "new", replace_all=replace_all)
    result = execute_tool("change_substring", args, state, _mock_catalog(), current_state_id="state-1")
    assert not result.success
    assert result.updated_state is state
    assert result.validation.errors[0].error_code == code


def test_later_failing_edit_changes_nothing() -> None:
    assert "change_substring" in _MUTATION_TOOL_NAMES
    state = _approved_state()
    text = state.nodes[0].options["prompt_template"]
    args = _edit(text, "Classify", "Assess")
    args["edits"].append({"old": "missing", "replacement": "new", "replace_all": False})
    result = execute_tool("change_substring", args, state, _mock_catalog(), current_state_id="state-1")
    assert not result.success
    assert result.updated_state is state
    assert "Classify" in state.nodes[0].options["prompt_template"]


def test_stale_state_or_hash_refuses_before_matching() -> None:
    assert "change_substring" in _MUTATION_TOOL_NAMES
    state = _approved_state()
    text = state.nodes[0].options["prompt_template"]
    for key, value in (("expected_state_id", "stale"), ("expected_sha256", "0" * 64)):
        args = _edit(text, "Classify", "Assess")
        args[key] = value
        result = execute_tool("change_substring", args, state, _mock_catalog(), current_state_id="state-1")
        assert not result.success and result.updated_state is state
        assert result.validation.errors[0].error_code == "target_changed"


def test_proposal_replay_rechecks_hash_matches_and_stored_artifact() -> None:
    assert "change_substring" in _MUTATION_TOOL_NAMES
    state = _approved_state()
    text = state.nodes[0].options["prompt_template"]
    args = _edit(text, "Classify", "Assess")
    args["expected_sha256"] = "0" * 64
    stale = execute_tool("change_substring", args, state, _mock_catalog(), executing_proposal_id="proposal-1")
    assert not stale.success and stale.updated_state is state
    assert stale.validation.errors[0].error_code == "target_changed"

    args = _edit(text, "absent", "Assess")
    unmatched = execute_tool("change_substring", args, state, _mock_catalog(), executing_proposal_id="proposal-1")
    assert not unmatched.success and unmatched.updated_state is state
    assert unmatched.validation.errors[0].error_code == "substring_not_found"

    args = _edit(text, "Classify", "Assess")
    corrupt = state.with_node(replace(state.nodes[0], options={**state.nodes[0].options, "approved_prompt_artifact_hash": "a" * 64}))
    guarded = execute_tool("change_substring", args, corrupt, _mock_catalog(), executing_proposal_id="proposal-1")
    assert not guarded.success and guarded.updated_state is corrupt
    assert guarded.validation.errors[0].error_code == "review_reconciliation_failed"


def test_overlap_and_replace_all_do_not_rematch_inserted_text() -> None:
    assert "change_substring" in _MUTATION_TOOL_NAMES
    from elspeth.web.composer.redaction import SubstringEditModel
    from elspeth.web.composer.tools.transforms import _apply_exact_substring_edits

    ambiguous = _apply_exact_substring_edits("aaa", [SubstringEditModel(old="aa", replacement="x")])
    assert ambiguous[0] is None and ambiguous[1][0] == "substring_not_unique"
    assert ambiguous[1][1] == "Edit 1 matched more than one location; provide a longer exact substring or explicitly set replace_all."
    assert _apply_exact_substring_edits("aaa", [SubstringEditModel(old="aa", replacement="x", replace_all=True)]) == ("xa", 1)
    assert _apply_exact_substring_edits("a", [SubstringEditModel(old="a", replacement="aa", replace_all=True)]) == ("aa", 1)


def test_exact_matching_bounds_overlap_scans() -> None:
    from elspeth.web.composer.redaction import SubstringEditModel
    from elspeth.web.composer.tools.transforms import _apply_exact_substring_edits

    class CountedText(str):
        find_calls = 0

        def find(self, sub: str, start: int = 0) -> int:
            self.find_calls += 1
            assert self.find_calls <= 2, "uniqueness scan examined more than two matches"
            return super().find(sub, start)

    ambiguous = CountedText("a" * 4096)
    result = _apply_exact_substring_edits(ambiguous, [SubstringEditModel(old="aa", replacement="x")])
    assert result[0] is None and result[1][0] == "substring_not_unique"
    assert ambiguous.find_calls == 2

    all_matches = CountedText("a" * 4096)
    result = _apply_exact_substring_edits(all_matches, [SubstringEditModel(old="aa", replacement="x", replace_all=True)])
    assert result == ("x" * 2048, 2048)
    assert all_matches.find_calls <= 2


def test_old_credential_removed_from_candidate_is_refused_before_handler() -> None:
    state = _approved_state()
    candidate = "sk-" + "a" * 24
    stored_text = f"Classify {candidate}"
    state = state.with_node(replace(state.nodes[0], options={**state.nodes[0].options, "prompt_template": stored_text}))
    args = _edit(stored_text, candidate, "removed")
    with pytest.raises(CredentialMaterialRefused):
        execute_tool("change_substring", args, state, _mock_catalog(), current_state_id="state-1")
    assert candidate in state.nodes[0].options["prompt_template"]


def test_intermediate_replacement_credential_is_refused_before_handler() -> None:
    state = _approved_state()
    text = state.nodes[0].options["prompt_template"]
    candidate = "sk-" + "a" * 24
    args = _edit(text, "Classify", candidate)
    args["edits"].append({"old": candidate, "replacement": "Assess", "replace_all": False})
    with pytest.raises(CredentialMaterialRefused):
        execute_tool("change_substring", args, state, _mock_catalog(), current_state_id="state-1")
    assert candidate not in state.nodes[0].options["prompt_template"]


def test_sequential_edits_and_utf8_limits() -> None:
    assert "change_substring" in _MUTATION_TOOL_NAMES
    from elspeth.web.composer.redaction import SubstringEditModel
    from elspeth.web.composer.tools.transforms import _apply_exact_substring_edits

    edits = [SubstringEditModel(old="one", replacement="two"), SubstringEditModel(old="two", replacement="three")]
    assert _apply_exact_substring_edits("one", edits) == ("three", 2)
    assert _apply_exact_substring_edits("one", [SubstringEditModel(old="", replacement="x")])[1][0] == "empty_old"
    assert _apply_exact_substring_edits("one", [SubstringEditModel(old="one", replacement="\ud800")])[1][0] == "invalid_utf8"
    assert _apply_exact_substring_edits("one", [SubstringEditModel(old="one", replacement="é" * 32769)])[1][0] == "edit_too_large"
    assert (
        _apply_exact_substring_edits("a" * 10000, [SubstringEditModel(old="a", replacement="x" * 64, replace_all=True)])[1][0]
        == "edited_text_too_large"
    )


def test_prompt_part_edit_preserves_reference_and_refusal_is_local() -> None:
    assert "change_substring" in _MUTATION_TOOL_NAMES
    state = _approved_state(structured=True)
    parts = state.nodes[0].options["prompt_template_parts"]
    text = parts[0]["text"]
    args = _edit(text, "Classify", "Assess")
    args["target"] = {"kind": "prompt_part", "node_id": "code_themes", "part_index": 0}
    result = execute_tool("change_substring", args, state, _mock_catalog(), current_state_id="state-1")
    assert result.success, result.validation.errors
    updated = result.updated_state.nodes[0].options["prompt_template_parts"]
    assert updated[0]["text"] == "Assess "
    assert updated[1] == parts[1]
    args["target"]["part_index"] = 1
    refused = execute_tool("change_substring", args, state, _mock_catalog(), current_state_id="state-1")
    assert not refused.success and refused.updated_state is state
    assert refused.validation.errors[0].error_code == "interpretation_slot"


def test_corrupt_approval_and_blob_arm_refuse_without_text() -> None:
    assert "change_substring" in _MUTATION_TOOL_NAMES
    state = _approved_state()
    text = state.nodes[0].options["prompt_template"]
    args = _edit(text, "Classify", "Assess")
    corrupt = state.with_node(replace(state.nodes[0], options={**state.nodes[0].options, "approved_prompt_artifact_hash": "a" * 64}))
    refused = execute_tool("change_substring", args, corrupt, _mock_catalog(), current_state_id="state-1")
    assert not refused.success and refused.updated_state is corrupt
    assert "Assess" not in repr(refused.to_dict())
    args["target"] = {"kind": "blob", "blob_id": "abc"}
    blob = execute_tool("change_substring", args, state, _mock_catalog(), current_state_id="state-1")
    assert not blob.success and blob.validation.errors[0].error_code == "blob_substring_not_supported"
    from elspeth.web.composer.tools._registry import resolve_tool_effects

    assert resolve_tool_effects("change_substring", args).domains == ()


def test_inspection_returns_hashes_and_part_indexes_without_content() -> None:
    assert "change_substring" in _MUTATION_TOOL_NAMES
    state = _approved_state(structured=True)
    result = execute_tool("inspect_prompt_targets", {"node_id": "code_themes"}, state, _mock_catalog(), current_state_id="state-1")
    assert result.success
    assert result.data["state_id"] == "state-1"
    indexes = [item["target"]["part_index"] for item in result.data["targets"] if item["target"]["kind"] == "prompt_part"]
    assert indexes == [0, 2]
    assert "Classify" not in repr(result.to_dict())
    assert all("sha256" in item for item in result.data["targets"])


def test_both_exact_edit_fragments_are_length_only_in_audit_arguments() -> None:
    assert "change_substring" in _MUTATION_TOOL_NAMES
    from elspeth.web.composer.redaction import redact_tool_call_arguments
    from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry

    args = _edit("Classify", "PRIVATE-OLD", "PRIVATE-NEW")
    redacted = redact_tool_call_arguments("change_substring", args, telemetry=NoopRedactionTelemetry())
    assert redacted["edits"][0]["old"] == "<inline-blob:11-bytes>"
    assert redacted["edits"][0]["replacement"] == "<inline-blob:11-bytes>"
    assert "PRIVATE" not in repr(redacted)


def test_effects_and_proposal_summary_expose_only_target_and_count() -> None:
    assert "change_substring" in _MUTATION_TOOL_NAMES
    from elspeth.web.composer.proposals import build_tool_proposal_summary
    from elspeth.web.composer.redaction import redact_tool_call_arguments
    from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
    from elspeth.web.composer.tools._registry import resolve_tool_effects
    from elspeth.web.composer.tools.declarations import EffectDomain

    args = _edit("Classify", "PRIVATE-OLD", "PRIVATE-NEW")
    redacted = redact_tool_call_arguments("change_substring", args, telemetry=NoopRedactionTelemetry())
    summary = build_tool_proposal_summary(tool_name="change_substring", arguments=args, redacted_arguments=redacted)
    assert "code_themes" in summary.summary and "1 exact edit" in summary.summary
    assert "PRIVATE" not in repr(summary)
    assert set(resolve_tool_effects("change_substring", args).domains) == {
        EffectDomain.GRAPH,
        EffectDomain.VALIDATION,
        EffectDomain.YAML,
        EffectDomain.INTERPRETATION,
    }


@pytest.mark.asyncio
async def test_batch_inspection_refuses_until_checkpoint_then_edit_can_continue(tmp_path: Path) -> None:
    import asyncio

    from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
    from elspeth.web.composer.anti_anchor import AntiAnchorTracker
    from elspeth.web.composer.audit import BufferingRecorder
    from elspeth.web.sessions.protocol import CompositionStateData
    from tests.unit.web.composer.test_service import _composer_service_with_session, _make_llm_response, _make_settings

    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    sessions = service._require_sessions_service()
    state = _approved_state()
    initial = state.to_dict()
    saved = await sessions.save_composition_state(
        UUID(session_id),
        CompositionStateData(
            nodes=initial["nodes"],
            sources=initial["sources"],
            edges=initial["edges"],
            outputs=initial["outputs"],
            metadata_=initial["metadata"],
            is_valid=False,
        ),
        provenance="session_seed",
    )

    async def batch(calls: list[dict[str, object]], current_state: object, current_id: str) -> object:
        snapshot, policy_catalog = service._policy_context.build(None)
        outcome, _ = await service._dispatch_tool_batch(
            call_model=_CallModelOutcome(completion=_make_llm_response(tool_calls=calls)),
            state=current_state,
            last_validation=None,
            last_runtime_preflight=None,
            llm_messages=[],
            recorder=BufferingRecorder(),
            anti_anchor=AntiAnchorTracker(),
            discovery_cache={},
            runtime_preflight_cache={},
            session_id=None,
            user_id=None,
            user_message_id=None,
            user_message_content=None,
            current_state_id=current_id,
            actor="composer-test",
            initial_version=current_state.version,
            deadline=asyncio.get_running_loop().time() + 60,
            progress=None,
            session_scope="prompt-edit-test",
            advisor_calls_used=0,
            composition_turns_used=0,
            discovery_turns_used=0,
            failed_turn=None,
            cancellation_requested=asyncio.Event(),
            plugin_snapshot=snapshot,
            policy_catalog=policy_catalog,
        )
        return outcome

    first_text = state.nodes[0].options["prompt_template"]
    first = await batch(
        [
            {
                "id": "edit-one",
                "name": "change_substring",
                "arguments": _edit(first_text, "Classify", "Assess") | {"expected_state_id": str(saved.id)},
            },
            {"id": "inspect-too-soon", "name": "inspect_prompt_targets", "arguments": {"node_id": "code_themes"}},
        ],
        state,
        str(saved.id),
    )
    assert first.state.nodes[0].options["prompt_template"] == first_text.replace("Classify", "Assess")
    assert first.tool_outcomes[0].response.success
    refused = first.tool_outcomes[1].response
    assert not refused.success
    assert refused.validation.errors[0].error_code == "inspect_after_uncheckpointed_mutation"
    assert "sha256" not in repr(refused.to_dict())

    changed = first.state.to_dict()
    checkpoint = await sessions.save_composition_state(
        UUID(session_id),
        CompositionStateData(
            nodes=changed["nodes"],
            sources=changed["sources"],
            edges=changed["edges"],
            outputs=changed["outputs"],
            metadata_=changed["metadata"],
            is_valid=False,
        ),
        provenance="tool_call",
    )
    second = await batch(
        [{"id": "inspect-after-checkpoint", "name": "inspect_prompt_targets", "arguments": {"node_id": "code_themes"}}],
        first.state,
        str(checkpoint.id),
    )
    inspection = second.tool_outcomes[0].response
    assert inspection is not None, repr(second.tool_outcomes)
    assert inspection.success and inspection.data["state_id"] == str(checkpoint.id)
    second_text = first.state.nodes[0].options["prompt_template"]
    assert inspection.data["targets"][0]["sha256"] == sha256(second_text.encode("utf-8")).hexdigest()
    third = await batch(
        [
            {
                "id": "edit-two",
                "name": "change_substring",
                "arguments": _edit(second_text, "Assess", "Review") | {"expected_state_id": str(checkpoint.id)},
            }
        ],
        first.state,
        str(checkpoint.id),
    )
    assert third.tool_outcomes[0].response.success
    assert third.state.nodes[0].options["prompt_template"] == second_text.replace("Assess", "Review")

    other_mutation = await batch(
        [
            {"id": "metadata-change", "name": "set_metadata", "arguments": {"patch": {"name": "Updated"}}},
            {"id": "inspect-after-metadata", "name": "inspect_prompt_targets", "arguments": {"node_id": "code_themes"}},
        ],
        first.state,
        str(checkpoint.id),
    )
    assert other_mutation.tool_outcomes[0].response.success
    assert other_mutation.tool_outcomes[1].response.validation.errors[0].error_code == "inspect_after_uncheckpointed_mutation"
