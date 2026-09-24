"""Bookkeeping checkpoints cannot replenish a broken graph's repair campaign."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer import service as service_module
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.state import CompositionState
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.test_interpretation_state import _pending_options, _state_with_llm

from ._helpers import _make_settings, _mock_catalog
from .test_runtime_preflight_pending_review_verification import (
    _nonempty_state,
    _RecordingValidatePipeline,
    _structural_failure_result,
    _valid_result,
)


@pytest.fixture
def service(tmp_path):
    return service_module.ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings(data_dir=tmp_path))


async def _attempt(service, state: CompositionState, *, user_id="user-1", session_scope="session:one", snapshot=None):
    messages = []
    fired = await service._attempt_preflight_repair(
        state=state,
        llm_messages=messages,
        user_id=user_id,
        session_id=None,
        last_runtime_preflight=None,
        runtime_preflight_cache=service._new_runtime_preflight_cache(),
        initial_version=state.version,
        session_scope=session_scope,
        recorder=SimpleNamespace(llm_calls=()),
        repair_turns_used=0,
        plugin_snapshot=snapshot,
    )
    assert len(messages) == int(fired)
    return fired


@pytest.mark.anyio
async def test_bookkeeping_checkpoint_does_not_replenish_repair_campaign(service, monkeypatch):
    fake = _RecordingValidatePipeline(strict=_structural_failure_result(), tolerant=_valid_result())
    monkeypatch.setattr(service_module, "validate_pipeline", fake)
    state = _nonempty_state(version=1)
    saved = replace(state, version=2)
    assert composition_content_hash(saved) == composition_content_hash(state)
    assert await _attempt(service, state)
    assert not await _attempt(service, state)
    assert not await _attempt(service, saved)
    assert len(fake.calls) == 3  # The current verdict is still recomputed every turn.
    assert service._runtime_preflight_key(state, session_scope="session:one", plugin_snapshot=None) != service._runtime_preflight_key(
        saved, session_scope="session:one", plugin_snapshot=None
    )


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["source", "output", "user", "session", "settings", "plugins"])
async def test_changed_repair_authority_gets_an_independent_campaign(service, monkeypatch, change):
    monkeypatch.setattr(
        service_module, "validate_pipeline", _RecordingValidatePipeline(strict=_structural_failure_result(), tolerant=_valid_result())
    )
    state = _nonempty_state(version=1)
    assert await _attempt(service, state)
    kwargs = {}
    if change == "source":
        source = state.sources["source"]
        options = dict(source.options)
        options["path"] = "different"
        state = replace(state, sources={"source": replace(source, options=options)})
    elif change == "output":
        state = replace(state, outputs=(replace(state.outputs[0], options={"path": "different.csv"}),))
    elif change == "user":
        kwargs["user_id"] = "user-2"
    elif change == "session":
        kwargs["session_scope"] = "session:two"
    elif change == "settings":
        monkeypatch.setattr(service_module, "runtime_preflight_settings_hash", lambda _settings: "changed-settings")
    else:
        snapshot = MagicMock(spec=PluginAvailabilitySnapshot)
        snapshot.snapshot_hash = "changed-plugins"
        kwargs["snapshot"] = snapshot
    assert await _attempt(service, state, **kwargs)
    assert not await _attempt(service, state, **kwargs)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["prompt_template", "review_draft", "secret_ref"])
async def test_node_and_review_control_changes_replenish_campaign(service, monkeypatch, change):
    monkeypatch.setattr(
        service_module, "validate_pipeline", _RecordingValidatePipeline(strict=_structural_failure_result(), tolerant=_valid_result())
    )
    node = _state_with_llm(_pending_options()).nodes[0]
    state = replace(_nonempty_state(version=1), nodes=(node,))
    assert await _attempt(service, state)
    assert not await _attempt(service, replace(state, version=2))
    options = deep_thaw(node.options)
    if change == "review_draft":
        options["interpretation_requirements"][0]["draft"] = "useful and documented"
    elif change == "secret_ref":
        options["api_key"] = {"secret_ref": "different-provider-key"}
    else:
        options["prompt_template"] = "Describe {{ row.text }}"
    changed = replace(state, nodes=(replace(node, options=options),))
    assert composition_content_hash(state) != composition_content_hash(changed)
    assert await _attempt(service, changed)
    assert not await _attempt(service, changed)
