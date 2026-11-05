"""Original ordinary read publications use nongraph P4 proof, never a graph locator."""

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_value_batch_preparation as preparation
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.sessions import continuation_value_batch_tail as tail
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.continuation import HistoricalEffectLocator
from elspeth.web.sessions.continuation_p4 import require_prepared_nongraph_p4_result
from elspeth.web.sessions.continuation_value_batch_read import read_value_batch_application_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composition_states_table as states
from elspeth.web.sessions.protocol import CompositionStateData
from tests.unit.web.sessions import test_continuation_live_phases as phases
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows
from tests.unit.web.sessions.test_continuation_v4_metadata_live import _v4_tool_case
from tests.unit.web.sessions.test_continuation_v4_pipeline_live import _PIPELINE_PLUGINS, _uploaded_source
from tests.unit.web.sessions.test_continuation_v4_pipeline_live import _arguments as pipeline_arguments
from tests.unit.web.sessions.test_continuation_value_batch_sql import _paid_rows, _prepare, _write

operation_store = phases.operation_store


async def _provider_authored_base(store, tmp_path, monkeypatch):
    blob = await _uploaded_source(store)
    output = Path(store[3]._data_dir) / "outputs" / str(store[4]) / "original-nongraph-base.json"
    with monkeypatch.context() as author_patch:
        async with _v4_tool_case(
            store,
            tmp_path,
            author_patch,
            tool="set_pipeline",
            arguments=pipeline_arguments(blob, output),
            available_plugins=_PIPELINE_PLUGINS,
        ) as (current, author, running, sdk):
            await phases._advance(author)
            assert author.latest_snapshot is not None and len(sdk) == 1
            with current[0].connect() as conn:
                row = conn.execute(
                    select(states).where(states.c.session_id == str(current[4])).order_by(states.c.version.desc()).limit(1)
                ).one()
            original = current[3]._row_to_state_record(row)
            assert original.sources is not None and original.outputs
            base = CompositionStateData(
                sources=original.sources,
                nodes=original.nodes,
                edges=original.edges,
                outputs=original.outputs,
                metadata_=original.metadata_,
                is_valid=original.is_valid,
                validation_errors=original.validation_errors,
                composer_meta=original.composer_meta,
            )
            cancelled = author.authority.request_cancel(session_id=current[4], operation_id=running.claim.operation_id)
            assert cancelled is not None and cancelled.cancel_requested_at is not None
            terminal = author.authority.settle_continuation_owner(running, outcome=request_cancelled_error(request_id=cancelled.request_id))
            assert terminal.status == "failed" and terminal.failure_code == "request_cancelled"
        assert author.lease.closed
    return base, terminal.operation_id


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_graph", (False, True), ids=("empty", "existing-provider-graph"))
async def test_original_read_only_value_batch_preserves_nongraph_receipt_and_cold_proof(
    operation_store, tmp_path, monkeypatch, existing_graph
):
    base, prior_operation = await _provider_authored_base(operation_store, tmp_path, monkeypatch) if existing_graph else (None, None)
    async with _v4_tool_case(
        operation_store,
        tmp_path,
        monkeypatch,
        tool="get_pipeline_state",
        base_state_data=base,
        prior_operation_id=prior_operation,
        available_plugins=_PIPELINE_PLUGINS if existing_graph else None,
    ) as (store, owner, running, sdk):
        batch = await _prepare(owner, running)
        paid_before = _paid_rows(store)
        with store[0].connect() as conn:
            states_before = tuple(conn.execute(select(states).where(states.c.session_id == str(store[4])).order_by(states.c.version)))
        assert bool(states_before) is existing_graph
        before_write = stored_rows(store[0])
        prepared = []
        original_prepare = tail.prepare_nongraph_p4_result_tail

        def observe_nongraph(*, selected, receipt, publication, published_at, env_ref_names):
            arguments = {
                "selected": selected,
                "receipt": receipt,
                "published_at": published_at,
                "env_ref_names": env_ref_names,
            }
            assert publication.issued_preview is None
            assert not publication.states and not publication.state_inputs
            assert receipt.checkpoint_disposition == ("no_new_checkpoint" if existing_graph else "empty")
            assert receipt.authored_state_hash is None and not receipt.inserted_state_ids and not receipt.state_row_hashes
            assert publication.retained_outcome.checkpoint is None
            with pytest.raises(AuditIntegrityError, match="requires its ordinary successful publication"):
                tail.prepare_p4_result_tail(**arguments)
            with pytest.raises(AuditIntegrityError, match="requires its original issued preview publication"):
                tail.prepare_issued_preview_p4_result_tail(**arguments, publication=publication)

            changed_message = replace(publication.messages[0], content=publication.messages[0].content + " altered")
            with pytest.raises(AuditIntegrityError, match="publication differs from its original source"):
                original_prepare(**arguments, publication=replace(publication, messages=(changed_message, *publication.messages[1:])))
            changed_source = replace(publication.source_identity, original_response_digest="0" * 64)
            with pytest.raises(AuditIntegrityError, match="publication differs from its original source"):
                original_prepare(**arguments, publication=replace(publication, source_identity=changed_source))
            changed_receipt = receipt.model_copy(update={"chat_message_hashes": ("0" * 64, *receipt.chat_message_hashes[1:])})
            with pytest.raises(AuditIntegrityError, match="changed its exact original receipt/input/row cohort"):
                original_prepare(**{**arguments, "receipt": changed_receipt}, publication=publication)
            invented_hash_receipt = receipt.model_copy(update={"authored_state_hash": "0" * 64})
            with pytest.raises(AuditIntegrityError, match=r"^tool continuation member violates its owned contract$") as invalid_receipt:
                original_prepare(**{**arguments, "receipt": invented_hash_receipt}, publication=publication)
            cause = invalid_receipt.value.__cause__
            assert type(cause) is ValidationError
            assert cause.errors(include_input=False, include_url=False, include_context=False) == [
                {
                    "type": "value_error",
                    "loc": (),
                    "msg": "Value error, P4 receipt invented a newly inserted checkpoint",
                }
            ]
            assert receipt.authored_state_hash is None and not receipt.inserted_state_ids and not receipt.state_row_hashes
            assert receipt.checkpoint_disposition == ("no_new_checkpoint" if existing_graph else "empty")

            result = original_prepare(**arguments, publication=publication)
            assert result.result.historical_effect is None and result.result.publication is publication
            require_prepared_nongraph_p4_result(result.result, env_ref_names=env_ref_names)
            with pytest.raises(
                AuditIntegrityError, match=r"^prepared nongraph P4 invented an authored checkpoint or changed its original publication$"
            ):
                require_prepared_nongraph_p4_result(replace(result.result, receipt=invented_hash_receipt), env_ref_names=env_ref_names)
            require_prepared_nongraph_p4_result(result.result, env_ref_names=env_ref_names)
            invented = HistoricalEffectLocator(
                receipt.effect_key, receipt.input_sha256, receipt.creator_operation_id, str(receipt.assistant_id), (), "0" * 64
            )
            with pytest.raises(AuditIntegrityError, match="requires its original value publication"):
                require_prepared_nongraph_p4_result(replace(result.result, historical_effect=invented), env_ref_names=env_ref_names)
            with pytest.raises(AuditIntegrityError, match="invented an authored checkpoint or changed its original publication"):
                require_prepared_nongraph_p4_result(
                    replace(result.result, outcome=result.result.outcome.model_copy(update={"checkpoint_disposition": "committed"})),
                    env_ref_names=env_ref_names,
                )
            prepared.append(result.result)
            return result

        monkeypatch.setattr(tail, "prepare_nongraph_p4_result_tail", observe_nongraph)
        actual_consume = tail.consume_value_batch_publication_on_connection
        original_failure = RuntimeError("original nongraph publication rollback")

        def rollback_after_publication(*args, **kwargs):
            actual_consume(*args, **kwargs)
            raise original_failure

        with monkeypatch.context() as rollback_patch:
            rollback_patch.setattr(tail, "consume_value_batch_publication_on_connection", rollback_after_publication)
            with pytest.raises(RuntimeError) as failed:
                _write(store, owner, running, batch)
            assert failed.value is original_failure
        assert prepared and stored_rows(store[0]) == before_write and _paid_rows(store) == paid_before
        stored = _write(store, owner, running, batch)
        with locked_session_transaction(store[0], str(store[4])) as conn:
            verified = read_value_batch_application_on_connection(
                conn, running, expected=stored.snapshot, env_ref_names=batch.inventory.names
            )
            states_after = tuple(conn.execute(select(states).where(states.c.session_id == str(store[4])).order_by(states.c.version)))
        assert states_after == states_before and _paid_rows(store) == paid_before
        assert verified.snapshot == stored.snapshot and len(verified.applications) == 1
        cold_result = verified.applications[0]
        assert cold_result.receipt == prepared[-1].receipt and cold_result.outcome == prepared[-1].outcome
        assert cold_result.historical_effect is None and not cold_result.receipt.inserted_state_ids

        async def forbidden(*args, **kwargs):
            raise AssertionError("cold nongraph result repeated actual value preparation")

        monkeypatch.setattr(preparation, "prepare_value_batch", forbidden)
        cold = ContinuationLivePhases(
            owner.service, owner.authority, owner.work, ProviderInvocationOwner(service=store[3], required_work=owner.work), owner.lease
        )
        selected = await load_selected_continuation(
            authority=owner.authority, running=running, selected=stored.snapshot, required_work=owner.work
        )
        before_cold = stored_rows(store[0])
        runtime, _inventory = await cold.current(selected)
        assert bool(runtime.live.state.sources) is existing_graph
        assert stored_rows(store[0]) == before_cold and len(sdk) == 1
