"""Actual V4 cold reconstruction controls for checkpoint detached capture.

These source/alias controls do not claim a checkpoint dispatch or SQL settlement.
"""

from copy import deepcopy
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardContentPart,
    detach_prompt_guard_evidence,
)
from elspeth.web.composer.continuation_checkpoint_projection import _require_detached_source
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.composer.continuation_runtime import restore_checkpoint_projection_runtime
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixture
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input

operation_store = test_composer_operation_authority.operation_store


async def _projection_case(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576))
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_fixture, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    original, work, _requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    assert type(original.source.policy) is ContinuationAdmissionPolicyV4
    detached = await restore_checkpoint_projection_runtime(
        service=original.materialization.service,
        authority=authority,
        selected=original.source.selected,
        inventory=original.materialization.inventory,
        required_work=work,
        provider_owner=original.bindings.provider_owner,
    )
    # Control the actual reconstructed graph before substituting an alias.
    _require_detached_source(detached, original)
    return original, detached, work


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alias", ("runtime", "live", "recorder", "messages", "message", "evidence", "anti_anchor", "materialization", "graph")
)
async def test_checkpoint_detached_source_rejects_original_mutable_alias(operation_store, tmp_path, monkeypatch, alias):
    original, detached, work = await _projection_case(operation_store, tmp_path, monkeypatch)
    before = original.live.recorder.snapshot_for_continuation(drained=original.drained)
    if alias == "runtime":
        changed = original
    elif alias == "live":
        changed = replace(detached, live=original.live)
    elif alias == "recorder":
        changed = replace(detached, live=replace(detached.live, recorder=original.live.recorder))
    elif alias == "messages":
        changed = replace(detached, live=replace(detached.live, llm_messages=original.live.llm_messages))
    elif alias == "message":
        messages = list(detached.live.llm_messages)
        messages[0] = original.live.llm_messages[0]
        changed = replace(detached, live=replace(detached.live, llm_messages=messages))
    elif alias == "evidence":
        changed = replace(detached, live=replace(detached.live, turn_evidence=original.live.turn_evidence))
    elif alias == "anti_anchor":
        changed = replace(detached, live=replace(detached.live, anti_anchor=original.live.anti_anchor))
    elif alias == "materialization":
        changed = replace(detached, materialization=original.materialization)
    else:
        changed = replace(detached, graph=original.graph)
    with pytest.raises(AuditIntegrityError, match="original mutable owner"):
        _require_detached_source(changed, original)
    assert original.live.recorder.snapshot_for_continuation(drained=original.drained) == before
    work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("remove_trust", "replace_partition"))
async def test_checkpoint_detached_source_rejects_changed_provenance_with_identical_wire(operation_store, tmp_path, monkeypatch, change):
    original, detached, work = await _projection_case(operation_store, tmp_path, monkeypatch)
    expected = detach_prompt_guard_evidence(detached.live.llm_messages)
    assert expected.trusted_message_indexes
    index = min(expected.trusted_message_indexes)
    messages = deepcopy(detached.live.llm_messages)
    trust = messages[index].pop(PROMPT_GUARD_TRUST_KEY)
    if change == "replace_partition":
        content = messages[index]["content"]
        assert type(content) is str
        messages[index][PROMPT_GUARD_CONTENT_PARTS_KEY] = (PromptGuardContentPart(content, trust=trust),)
    observed = detach_prompt_guard_evidence(messages)
    assert observed.messages == expected.messages
    assert observed != expected
    changed = replace(detached, live=replace(detached.live, llm_messages=messages))
    with pytest.raises(AuditIntegrityError, match="complete retained pre-call source"):
        _require_detached_source(changed, original)
    assert detach_prompt_guard_evidence(original.live.llm_messages) == expected
    work.coordinator.assert_completed()
