"""EARLY SDK dispatch derives its family from the original physical/SQL source."""

from dataclasses import replace

import litellm
import pytest
from openai import OpenAIError
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_checkpoint_early_physical import checkpoint_early_physical_custody
from elspeth.web.composer.provider_quota import V4SdkTerminalCommitted
from elspeth.web.sessions.continuation_sdk_dispatch import sdk_terminal_family_for_custody
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily
from elspeth.web.sessions.continuation_sdk_terminal_cold import read_v4_sdk_terminal_verified_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import _early_attempt, _early_case, _invoke_early
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import operation_store as _operation_store

operation_store = _operation_store


@pytest.mark.asyncio
async def test_early_sdk_dispatch_replays_original_family_and_refuses_substitutions(operation_store, tmp_path, monkeypatch):
    async with _early_case(operation_store, tmp_path, monkeypatch) as (store, owner, running, selected, primary_sdk):
        calls = []

        async def failed_completion(**_request):
            calls.append(1)
            raise OpenAIError("original EARLY provider failure")

        monkeypatch.setattr(litellm, "acompletion", failed_completion)
        async with _early_attempt(owner, selected) as source:
            with pytest.raises(V4SdkTerminalCommitted) as raised:
                await _invoke_early(source)
            custody = checkpoint_early_physical_custody(source)
            assert custody.v4_sdk_terminal_result is raised.value.finalization
            verified = raised.value.finalization.verified
            assert sdk_terminal_family_for_custody(custody) is SdkTerminalFamily.CHECKPOINT_EARLY
            assert calls == [1] and len(primary_sdk) == 1
            admission = custody.continuation_checkpoint_v4_admission
            assert admission is not None
            try:
                custody.continuation_checkpoint_v4_admission = object()
                with pytest.raises(AuditIntegrityError, match="no installed nominal admission"):
                    sdk_terminal_family_for_custody(custody)
                custody.continuation_checkpoint_v4_admission = replace(admission, original_source=object())
                with pytest.raises(AuditIntegrityError, match="nominal original source"):
                    sdk_terminal_family_for_custody(custody)
                custody.continuation_checkpoint_v4_admission = replace(
                    admission, original_source=replace(admission.original_source, phase="end")
                )
                with pytest.raises(AuditIntegrityError, match="original phase/input"):
                    sdk_terminal_family_for_custody(custody)
                custody.continuation_checkpoint_v4_admission = replace(
                    admission, original_source=replace(admission.original_source, early_input_ref=None)
                )
                with pytest.raises(AuditIntegrityError, match="original phase/input"):
                    sdk_terminal_family_for_custody(custody)
            finally:
                custody.continuation_checkpoint_v4_admission = admission
            assert sdk_terminal_family_for_custody(custody) is SdkTerminalFamily.CHECKPOINT_EARLY

        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with locked_session_transaction(store[0], sid) as conn:

            def read(family=SdkTerminalFamily.CHECKPOINT_EARLY):
                return read_v4_sdk_terminal_verified_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=operation,
                    effect_key=verified.source.effect_key,
                    family=family,
                    names=verified.env_ref_names,
                )

            assert read() == verified and verified.hold_state == "consumed"
            with pytest.raises(AuditIntegrityError, match="requested family"):
                read(SdkTerminalFamily.CHECKPOINT_END)
            assert read() == verified
            outer = conn.execute(
                select(effects.c.effect_key).where(
                    effects.c.session_id == sid,
                    effects.c.operation_id == operation,
                    effects.c.owner == "composer_iteration",
                    effects.c.disposition == "unknown",
                )
            ).scalar_one()
            savepoint = conn.begin_nested()
            try:
                changed = conn.execute(
                    update(effects)
                    .where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == outer)
                    .values(disposition="dispatched")
                )
                assert changed.rowcount == 1
                with pytest.raises(AuditIntegrityError, match="outer vector"):
                    read()
            finally:
                savepoint.rollback()
            assert read() == verified
