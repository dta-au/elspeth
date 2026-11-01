"""Live Continuation progress through the production app, worker and HTTP read.

The private progress-owner tests exercise its local lease protocol. This test
requires the actual segment driver to bind that owner to current SQL authority
and to retire it after the physical operation finishes.
"""

from __future__ import annotations

import asyncio
import sys
from uuid import uuid4

import pytest

from elspeth.web.composer.progress import ComposerProgressSnapshot
from elspeth.web.sessions.schemas import ComposerOperationSnapshot
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    PRIMARY_MODEL,
    ScriptedSDK,
    assistant_contents,
    continuation_app,
    end_admit,
    install_sdk,
    sdk_reply,
    settle,
)


@pytest.mark.asyncio
async def test_live_primary_progress_binds_current_operation_and_retires_after_terminal(tmp_path, monkeypatch) -> None:
    entered_primary = asyncio.Event()
    release_primary = asyncio.Event()
    provider_words = "The current pipeline is ready for review."
    end_sdk = ScriptedSDK([("end", end_admit())])

    async def completion(**kwargs: object) -> object:
        if kwargs["model"] == PRIMARY_MODEL:
            assert kwargs["tools"], "the actual PRIMARY must carry its model palette"
            entered_primary.set()
            await release_primary.wait()
            return sdk_reply(provider_words)
        return await end_sdk(**kwargs)

    install_sdk(monkeypatch, completion)
    operation_id = str(uuid4())
    async with continuation_app(tmp_path, monkeypatch) as harness:
        registry = harness.app.state.composer_progress_registry
        authority = harness.app.state.composer_async_operation_authority
        async with harness.client() as client:
            before_response = await client.get(f"/api/sessions/{harness.session_id}/composer-progress")
            assert before_response.status_code == 200, before_response.text
            before = ComposerProgressSnapshot.model_validate_json(before_response.content, strict=True)
            assert before.phase == "idle" and before.request_token is None

            accepted = await client.post(
                f"/api/sessions/{harness.session_id}/messages",
                json=message_body("Explain the current pipeline.", operation_id=operation_id),
            )
            assert accepted.status_code == 202, accepted.text
            try:
                await asyncio.wait_for(entered_primary.wait(), timeout=45)
                record = authority.get_for_turn(session_id=harness.session_id, operation_id=operation_id)
                assert record is not None and record.status == "running"
                assert record.session_operation_id is not None and record.session_operation_epoch is not None

                operation_response = await client.get(f"/api/sessions/{harness.session_id}/operations/{operation_id}")
                assert operation_response.status_code == 200, operation_response.text
                operation = ComposerOperationSnapshot.model_validate_json(operation_response.content, strict=True)
                assert operation.status == "running" and operation.segment is not None
                assert (operation.segment.sequence, operation.segment.generation) == (
                    record.current_segment_sequence,
                    record.ownership_generation,
                )

                progress_response = await client.get(f"/api/sessions/{harness.session_id}/composer-progress")
                assert progress_response.status_code == 200, progress_response.text
                progress = ComposerProgressSnapshot.model_validate_json(progress_response.content, strict=True)
                assert progress.phase == "calling_model"
                assert progress.session_id == str(harness.session_id)
                assert progress.request_id == str(record.reserved_user_message_id)
                assert progress.operation_id == operation_id
                assert (progress.session_operation_id, progress.session_operation_epoch) == (
                    record.session_operation_id,
                    record.session_operation_epoch,
                )
                assert progress.request_token is not None and progress.inflight_requests == 1
                current = await registry.get_for_operation(
                    session_id=str(harness.session_id),
                    user_id=record.actor_user_id,
                    operation_id=operation_id,
                    session_operation_id=record.session_operation_id,
                    session_operation_epoch=record.session_operation_epoch,
                )
                assert current is not None and current.request_token == progress.request_token
            finally:
                release_primary.set()
                if entered_primary.is_set():
                    # Join only the actual started owner. Running the worker's
                    # claim loop here can hang again if the assertion failed
                    # before PRIMARY; app lifespan owns that bounded drain.
                    worker = harness.app.state.composer_async_worker
                    job = worker._jobs.get((harness.session_id, operation_id))
                    if job is not None:
                        original = sys.exception()
                        try:
                            finished, _pending = await asyncio.wait({job}, timeout=30.0)
                            if job not in finished:
                                raise TimeoutError("Continuation owner did not finish within 30 seconds after PRIMARY release")
                        except BaseException as cleanup:
                            if original is not None:
                                raise BaseExceptionGroup(
                                    "Progress assertion and bounded owner cleanup both failed", [original, cleanup]
                                ) from None
                            raise

            terminal = await settle(harness, operation_id=operation_id, client=client)
            assert terminal.status == "completed" and terminal.error is None and terminal.result is not None
            assert terminal.result.message.content.split("\n\n---\n\n")[0] == provider_words
            assert assistant_contents(harness.app, harness.session_id) == [terminal.result.message.content]
            assert end_sdk.roles == ["end"] and not end_sdk.steps
            after_response = await client.get(f"/api/sessions/{harness.session_id}/composer-progress")
            assert after_response.status_code == 200, after_response.text
            after = ComposerProgressSnapshot.model_validate_json(after_response.content, strict=True)
            assert after.phase == "complete" and after.inflight_requests == 0
