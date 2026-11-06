"""Actual V4 planner-authored graph, whole-tail admission and cold P4."""

from pathlib import Path

import pytest
from sqlalchemy import select

from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.tools._common import _validate_sink_path, validate_composer_file_sink_collision_policy
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.plugin_policy.models import PluginId
from elspeth.web.sessions.continuation_next_iteration import is_applied_batch_cut
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult
from elspeth.web.sessions.continuation_tool_no_execution import ToolNoExecutionRefusal, read_tool_no_execution_refusal_on_connection
from elspeth.web.sessions.models import blobs_table as blobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composition_states_table as states
from tests.unit.web.sessions import test_continuation_live_phases as phase_fixture
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows
from tests.unit.web.sessions.test_continuation_v4_metadata_live import _forbid_execution, _v4_tool_case

operation_store = phase_fixture.operation_store
_PIPELINE_PLUGINS = frozenset((PluginId("source", "csv"), PluginId("sink", "json")))


async def _uploaded_source(store):
    engine, repository, _authority, sessions, sid = store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    try:
        owner = BlobServiceImpl(engine, Path(sessions._data_dir), session_operation_authority=sessions.session_operation_authority)
        blob = await owner.create_blob(sid, "original-user-data.csv", b"value\n1\n", "text/csv", session_operation_context=context)
        assert blob.status == "ready"
        return blob
    finally:
        repository.release(context)


def _arguments(blob, output_path):
    return {
        "source": {
            "plugin": "csv",
            "blob_id": str(blob.id),
            "options": {"schema": {"mode": "observed"}},
            "on_success": "original_model_output",
            "on_validation_failure": "discard",
        },
        "nodes": [],
        "edges": [],
        "outputs": [
            {
                "sink_name": "original_model_output",
                "plugin": "json",
                "options": {
                    "path": str(output_path),
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "fail_if_exists",
                },
                "on_write_failure": "discard",
            }
        ],
    }


@pytest.mark.asyncio
async def test_actual_v4_planner_pipeline_applies_full_p4_and_cold_restore_never_reexecutes(operation_store, tmp_path, monkeypatch):
    blob = await _uploaded_source(operation_store)
    output_path = Path(operation_store[3]._data_dir) / "outputs" / str(operation_store[4]) / "original-model-output.json"
    arguments = _arguments(blob, output_path)
    assert (
        _validate_sink_path(
            {"path": str(tmp_path / "original-model-output.json")}, str(operation_store[3]._data_dir), session_id=str(operation_store[4])
        )
        is not None
    )
    assert (
        _validate_sink_path(arguments["outputs"][0]["options"], str(operation_store[3]._data_dir), session_id=str(operation_store[4]))
        is None
    )
    assert (
        validate_composer_file_sink_collision_policy(
            "json", {"path": str(output_path), "schema": {"mode": "observed"}}, require_explicit=True
        )
        is not None
    )
    assert validate_composer_file_sink_collision_policy("json", arguments["outputs"][0]["options"], require_explicit=True) is None
    async with _v4_tool_case(
        operation_store, tmp_path, monkeypatch, tool="set_pipeline", arguments=arguments, available_plugins=_PIPELINE_PLUGINS
    ) as (
        store,
        owner,
        running,
        sdk,
    ):
        entered = _forbid_execution(monkeypatch)
        with store[0].connect() as conn:
            before_states = conn.execute(select(states).where(states.c.session_id == str(store[4]))).all()
            before_blobs = conn.execute(select(blobs).where(blobs.c.session_id == str(store[4]))).all()
        await phase_fixture._advance(owner)
        stored = owner.latest_snapshot
        assert owner.tool_refusal is None
        assert stored is not None and owner.runtime is owner.primary is owner.p4 is owner.finished_batch is None
        with store[0].connect() as conn:
            after_states = conn.execute(select(states).where(states.c.session_id == str(store[4]))).all()
            after_blobs = conn.execute(select(blobs).where(blobs.c.session_id == str(store[4]))).all()
            actual_effects = conn.execute(select(effects).where(effects.c.session_id == str(store[4]))).all()
        assert len(after_states) == len(before_states) + 1 and after_blobs == before_blobs
        assert actual_effects and all(row.disposition == "committed" for row in actual_effects)
        selected = await load_selected_continuation(authority=owner.authority, running=running, selected=stored, required_work=owner.work)
        assert is_applied_batch_cut(selected)
        cold = ContinuationLivePhases(
            owner.service, owner.authority, owner.work, ProviderInvocationOwner(service=store[3], required_work=owner.work), owner.lease
        )
        before_restore = stored_rows(store[0])
        runtime, _inventory = await cold.current(selected)
        state = runtime.live.state
        assert state.sources["source"].plugin == "csv" and state.sources["source"].options["blob_ref"] == str(blob.id)
        assert not state.nodes and not state.edges
        assert len(state.outputs) == 1 and state.outputs[0].name == "original_model_output"
        assert state.outputs[0].options["mode"] == "write" and state.outputs[0].options["collision_policy"] == "fail_if_exists"
        assert runtime.graph.batch is not None and state is runtime.graph.batch.batch.state
        assert stored_rows(store[0]) == before_restore and len(sdk) == 1 and not entered


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported", ("inline_blob", "second_call"))
async def test_actual_v4_pipeline_unsupported_tail_is_cold_unissued_without_graph_or_blob_writes(
    operation_store, tmp_path, monkeypatch, unsupported
):
    blob = await _uploaded_source(operation_store)
    output_path = Path(operation_store[3]._data_dir) / "outputs" / str(operation_store[4]) / "original-model-output.json"
    arguments = _arguments(blob, output_path)
    following = None
    if unsupported == "inline_blob":
        arguments["source"].pop("blob_id")
        arguments["source"]["inline_blob"] = {
            "filename": "unissued.json",
            "mime_type": "application/json",
            "content": '[{"value": 2}]',
        }
    else:
        following = ("unissued_suffix", "set_metadata", {"patch": {"name": "unissued suffix"}})
    async with _v4_tool_case(
        operation_store,
        tmp_path,
        monkeypatch,
        tool="set_pipeline",
        arguments=arguments,
        following_tool_call=following,
        available_plugins=_PIPELINE_PLUGINS,
    ) as (store, owner, running, sdk):
        entered = _forbid_execution(monkeypatch)
        selected, inventory = await phase_fixture._finish_inventory(owner, running)
        primary = owner.primary
        assert type(primary) is PrimaryProviderResult
        with store[0].connect() as conn:
            before_states = conn.execute(select(states).where(states.c.session_id == str(store[4]))).all()
            before_blobs = conn.execute(select(blobs).where(blobs.c.session_id == str(store[4]))).all()
        receipt = await owner._execute_one(selected)
        assert type(receipt) is ToolNoExecutionRefusal
        with store[0].connect() as conn:
            cold = read_tool_no_execution_refusal_on_connection(
                conn,
                session_id=store[4],
                operation_id=running.claim.operation_id,
                effect_key=receipt.effect_key,
                env_ref_names=inventory.names,
            )
            after_states = conn.execute(select(states).where(states.c.session_id == str(store[4]))).all()
            after_blobs = conn.execute(select(blobs).where(blobs.c.session_id == str(store[4]))).all()
            actual_effects = conn.execute(select(effects).where(effects.c.session_id == str(store[4]))).all()
        assert cold == receipt and after_states == before_states and after_blobs == before_blobs
        assert len(actual_effects) == 2
        provider = tuple(row for row in actual_effects if row.owner == "provider")
        refused = tuple(row for row in actual_effects if row.owner == "tool_dispatch")
        assert len(provider) == len(refused) == 1
        assert provider[0].effect_key == primary.effect_key and provider[0].disposition == "committed"
        assert (refused[0].effect_key, refused[0].disposition, refused[0].receipt_key) == (
            receipt.effect_key,
            "refused",
            receipt.receipt_key,
        )
        assert refused[0].input_sha256 == receipt.input_sha256 and refused[0].provider_attempt_id is None
        assert len(sdk) == 1 and not entered
