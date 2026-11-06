"""Actual empty producer observations and pure preview DATA planning controls.

Nonempty dispatch is deliberately absent: encoding DATA grants no producer,
current SQL or terminal permission.
"""

from copy import copy
from dataclasses import replace
from hashlib import sha256

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_preview_observation as observation
from elspeth.web.composer import continuation_value_batch_preview as preview
from elspeth.web.composer.continuation_codec import PrivateChunk
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from tests.unit.web.sessions import test_continuation_value_batch_preview as preview_fixture
from tests.unit.web.sessions.test_continuation_value_batch_preview import (
    forbid_new_resources,
    prepared_preview,
)

operation_store = preview_fixture.operation_store


@pytest.mark.asyncio
async def test_preview_observes_original_empty_return_and_cold_codec_without_reexecution(operation_store, tmp_path, monkeypatch):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (_, _, prepared, sdk):
        source = observation.preview_source_data(prepared)
        entered = forbid_new_resources(monkeypatch)
        original = await preview.observe_empty_preview_value(prepared)
        assert original.source == source
        assert original.result.updated_state is prepared.call.pre_state
        assert original.strict is original.result.runtime_preflight and original.tolerant is None
        observation.require_original_preview_observation(original, prepared)
        assert original.data.source.prepared_batch_sha256 == sha256(prepared.prepared_batch_json.encode("utf-8")).hexdigest()
        assert original.data.source.raw_arguments_sha256 == sha256(prepared.call.tool_call.function.arguments.encode("utf-8")).hexdigest()
        chunk = observation.encode_preview_data(original.data, env_ref_names=prepared.runtime.env_ref_names)

        def no_reexecution(*args, **kwargs):
            raise AssertionError("cold preview DATA attempted producer or dispatcher replay")

        monkeypatch.setattr(preview, "execute_tool", no_reexecution)
        monkeypatch.setattr(preview.ComposerPreflight, "cached_runtime_preflight", no_reexecution)
        replay = observation.decode_preview_data(chunk, expected_source=source, env_ref_names=prepared.runtime.env_ref_names)
        assert replay == original.data and type(replay) is observation.PreviewObservationData
        assert type(replay) is not observation.OriginalPreviewObservation
        assert chunk.canonical == canonical_json(original.data.model_dump(mode="json"))
        assert not entered and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["copy", "constructor", "result_mutation"])
async def test_preview_requires_issued_identity_and_unchanged_complete_original(mode, operation_store, tmp_path, monkeypatch):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (_, _, prepared, _):
        original = await preview.observe_empty_preview_value(prepared)
        if mode == "copy":
            candidate = copy(original)
        elif mode == "constructor":
            candidate = replace(original)
        else:
            original.strict.checks[0].detail = "changed original returned diagnostic"
            candidate = original
        with pytest.raises(AuditIntegrityError, match="original"):
            observation.require_original_preview_observation(candidate, prepared)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["scope", "index", "phase", "audit_copy"])
async def test_preview_rechecks_original_prepared_source_before_accepting_returned_data(mode, operation_store, tmp_path, monkeypatch):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (_, _, prepared, _):
        original = await preview.observe_empty_preview_value(prepared)
        if mode == "scope":
            prepared = replace(prepared, context=replace(prepared.context, session_scope="foreign-session-scope"))
        elif mode == "index":
            prepared.call.index += 1
        elif mode == "phase":
            prepared.call.phase = "application_pending"
        else:
            prepared.call.audit = replace(prepared.original_audit)
        with pytest.raises(AuditIntegrityError):
            observation.require_original_preview_observation(original, prepared)


@pytest.mark.asyncio
async def test_preview_byte_plan_is_exact_at_limit_and_plus_one_without_whole_tail_claim(operation_store, tmp_path, monkeypatch):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (_, _, prepared, _):
        original = await preview.observe_empty_preview_value(prepared)
        encoded_bytes = canonical_json(original.data.model_dump(mode="json")).encode("utf-8")
        exact = len(encoded_bytes)
        assert exact < CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        assert observation.measure_preview_data(original.data, limit=exact).admitted_bytes == exact
        exceeded = observation.measure_preview_data(original.data, limit=exact - 1)
        assert exceeded.exceeded and exceeded.admitted_bytes is None and exceeded.limit == exact - 1
        assert observation.measure_preview_data(original.data).admitted_bytes == exact
        # A caller cannot turn a size observation into a live result witness.
        with pytest.raises(AuditIntegrityError, match="issued identity"):
            observation.require_original_preview_observation(replace(original), prepared)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreign_source", "noncanonical", "changed_bytes"])
async def test_preview_cold_codec_refuses_foreign_source_and_changed_complete_bytes(mode, operation_store, tmp_path, monkeypatch):
    async with prepared_preview(operation_store, tmp_path, monkeypatch) as (_, _, prepared, _):
        original = await preview.observe_empty_preview_value(prepared)
        chunk = observation.encode_preview_data(original.data, env_ref_names=prepared.runtime.env_ref_names)
        source = original.source
        if mode == "foreign_source":
            source = source.model_copy(update={"checkpoint_id": "foreign-checkpoint"})
        elif mode == "noncanonical":
            chunk = PrivateChunk(chunk.reference, " " + chunk.canonical)
        else:
            chunk = PrivateChunk(chunk.reference, chunk.canonical.replace("empty_pipeline", "other_pipeline"))
        with pytest.raises(AuditIntegrityError):
            observation.decode_preview_data(chunk, expected_source=source, env_ref_names=prepared.runtime.env_ref_names)
