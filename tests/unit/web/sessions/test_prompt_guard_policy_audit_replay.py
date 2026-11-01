"""Guard audit schema compatibility through the real failed Advisor reader."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import Text, select, text, update
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import is_lower_sha256_hex
from elspeth.web.composer import prompt_guard
from elspeth.web.sessions.continuation_tool_failure import read_tool_failure_source_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from tests.unit.web.sessions.test_continuation_tool_failure import _advance, _advisor_case
from tests.unit.web.sessions.test_continuation_tool_failure import operation_store as _operation_store

operation_store = _operation_store


class _TrustedCorruptAuditContent(TypeDecorator[str]):
    """Expose corrupt SQL content to the reader without changing stored bytes.

    The actual append-only trigger stays active. Only this disposable test's
    selected original audit content crosses a deliberately corrupt read seam;
    all original row identifiers, source selection and reader checks remain real.
    """

    impl = Text
    cache_ok = False

    def __init__(self, *, original: str, replacement: str, observed: list[str]):
        super().__init__()
        self.original = original
        self.replacement = replacement
        self.observed = observed

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        if value == self.original:
            self.observed.append(value)
            return self.replacement
        return value


def _schema1_request_hash(arguments):
    """The complete request encoding of the genuine 339b schema-1 producer.

    Keep the legacy input binding, rather than deleting a schema-2 identity
    from its audit while leaving a schema-2 request hash behind.
    """
    wire = arguments["messages"]
    parts = arguments["content_parts"]
    arguments_parts = arguments["tool_argument_parts"]
    thinking = arguments["thinking_parts"]
    exact = {
        "role": arguments["role"],
        "policy_fingerprint": arguments["policy_fingerprint"],
        "enforce_azure_character_limits": arguments["enforce_azure_character_limits"],
        "user_prompt": prompt_guard.resolve_prompt_guard_user_prompt(arguments["user_prompt"]),
        "messages": list(wire),
        "tools": list(arguments["tools"]),
        "trusted_message_indexes": sorted(arguments["trusted_message_indexes"]),
        "content_parts": [
            None
            if entry is None
            else [
                {"text": part.text, "trust": part.trust.value if part.trust is not None else None, "inspection_text": part.inspection_text}
                for part in entry
            ]
            for entry in (tuple(None for _ in wire) if parts is None else parts)
        ],
        "tool_argument_parts": [
            None
            if entries is None
            else [
                {
                    "tool_call_index": entry.tool_call_index,
                    "parts": [{"text": part.text, "inspection_text": part.inspection_text} for part in entry.parts],
                }
                for entry in entries
            ]
            for entries in (tuple(None for _ in wire) if arguments_parts is None else arguments_parts)
        ],
    }
    thinking = tuple(None for _ in wire) if thinking is None else thinking
    if any(entry is not None for entry in thinking):
        exact["thinking_parts"] = [
            None if entry is None else [{"text": part.text, "inspection_text": part.inspection_text} for part in entry.parts]
            for entry in thinking
        ]
    encoded = json.dumps(exact, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _install_schema1_writer(monkeypatch):
    """Exercise frozen legacy encoding with real inspection and fenced writes.

    The request/call/creator/decision are actual. Only this format adapter is
    historical; neither a new guard decision nor a fake failure is invented.
    """
    actual_request, actual_persist = prompt_guard._request, prompt_guard._persist
    original_hashes = []

    def request(**arguments):
        result, supported = actual_request(**arguments)
        legacy_hash = _schema1_request_hash(arguments)
        if "validity_policy_fingerprint" not in arguments:
            assert result.request_hash == legacy_hash
        if arguments["role"] == "advisor":
            original_hashes.append(legacy_hash)
            return replace(result, request_hash=legacy_hash), supported
        return result, supported

    async def persist(*, custody, envelope):
        check = envelope["check"]
        if check["role"] == "advisor":
            # Enumerate the original schema; do not relabel a V2 request hash.
            check = {
                "schema_version": 1,
                "audit_id": check["audit_id"],
                "operation_id": check["operation_id"],
                "operation_epoch": check["operation_epoch"],
                "role": check["role"],
                "adapter": check["adapter"],
                "mode": check["mode"],
                "policy_fingerprint": check["policy_fingerprint"],
                "request_hash": check["request_hash"],
                "disposition": check["disposition"],
                "reason": check["reason"],
                "started_at": check["started_at"],
                "duration_ms": check["duration_ms"],
            }
            assert check["request_hash"] == original_hashes[-1]
            envelope = {"_kind": "prompt_guard_audit", "check": check}
        await actual_persist(custody=custody, envelope=envelope)

    monkeypatch.setattr(prompt_guard, "_request", request)
    monkeypatch.setattr(prompt_guard, "_persist", persist)
    return original_hashes


@asynccontextmanager
async def _failed_guard_case(store, tmp_path, monkeypatch, *, legacy):
    with monkeypatch.context() as guard_patch:
        legacy_hashes = _install_schema1_writer(guard_patch) if legacy else None
        async with _advisor_case(store, tmp_path, monkeypatch, block=True) as (owner, running, sdk):
            await _advance(owner)
            retained = owner.runtime.graph.batch.batch.retained_failure
            assert retained is not None and retained.refusal_source is not None
            assert retained.refusal_source.kind == "prompt_guard_refused"
            audit_id = retained.refusal_source.outcome.response["audit_id"]
            with store[0].connect() as conn:
                row = conn.execute(
                    select(messages).where(
                        messages.c.session_id == str(running.claim.session_id),
                        messages.c.tool_calls[0]["check"]["audit_id"].as_string() == audit_id,
                    )
                ).one()
            envelope = row.tool_calls[0]
            assert json.loads(row.content) == envelope
            assert envelope["check"]["disposition"] == "blocked"
            assert len(sdk) == 1
            if legacy:
                assert legacy_hashes and envelope["check"]["request_hash"] == legacy_hashes[-1]
            yield owner, running, sdk, row


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", [1, 2])
async def test_cold_guard_failure_accepts_exact_policy_schema_without_inventing_legacy_identity(
    operation_store, tmp_path, monkeypatch, schema
):
    async with _failed_guard_case(operation_store, tmp_path, monkeypatch, legacy=schema == 1) as (owner, running, sdk, row):
        before = deepcopy(row.tool_calls)
        check = row.tool_calls[0]["check"]
        assert check["schema_version"] == schema
        assert is_lower_sha256_hex(check["policy_fingerprint"])
        if schema == 1:
            assert "validity_policy_fingerprint" not in check
        else:
            assert is_lower_sha256_hex(check["validity_policy_fingerprint"])
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            source = read_tool_failure_source_on_connection(conn, running, env_ref_names=owner.runtime.env_ref_names)
            assert source is not None and not source.applied
            assert source.invocation == owner.failure_custody.invocation
            unchanged = conn.execute(select(messages).where(messages.c.id == row.id)).one()
            assert unchanged.tool_calls == before and unchanged.content == row.content
        assert len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "missing-validity",
        "invalid-validity",
        "invalid-adapter",
        "extra-field",
        "wrong-version-type",
        "downgrade-with-v2-field",
        "content-disagreement",
        "creator-epoch",
        "disposition",
    ],
)
async def test_schema2_cold_guard_failure_refuses_corrupt_policy_or_original_audit(operation_store, tmp_path, monkeypatch, change):
    async with _failed_guard_case(operation_store, tmp_path, monkeypatch, legacy=False) as (owner, running, sdk, row):
        assert row.tool_calls[0]["check"]["schema_version"] == 2
        assert is_lower_sha256_hex(row.tool_calls[0]["check"]["validity_policy_fingerprint"])
        sid = str(running.claim.session_id)
        with locked_session_transaction(operation_store[0], sid) as conn:
            assert read_tool_failure_source_on_connection(conn, running, env_ref_names=owner.runtime.env_ref_names) is not None
            with conn.begin_nested() as savepoint:
                envelope = deepcopy(row.tool_calls[0])
                check = envelope["check"]
                if change == "missing-validity":
                    del check["validity_policy_fingerprint"]
                elif change == "invalid-validity":
                    check["validity_policy_fingerprint"] = "not-a-sha256-policy"
                elif change == "invalid-adapter":
                    check["policy_fingerprint"] = "not-a-sha256-policy"
                elif change == "extra-field":
                    check["unexpected_policy"] = "0" * 64
                elif change == "wrong-version-type":
                    check["schema_version"] = "2"
                elif change == "downgrade-with-v2-field":
                    check["schema_version"] = 1
                elif change == "creator-epoch":
                    check["operation_epoch"] += 1
                elif change == "disposition":
                    check["disposition"], check["reason"] = "allow", "clean"
                else:
                    assert change == "content-disagreement"
                    check["validity_policy_fingerprint"] = "0" * 64
                content = row.content if change == "content-disagreement" else json.dumps(envelope, sort_keys=True)
                assert conn.execute(update(messages).where(messages.c.id == row.id).values(tool_calls=[envelope])).rowcount == 1
                # Metadata is actually mutable; content is append-only. Corrupt
                # only its trusted SQL result processor, never the DB trigger.
                observed = []
                with monkeypatch.context() as corrupt_read:
                    corrupt_read.setattr(
                        messages.c.content,
                        "type",
                        _TrustedCorruptAuditContent(original=row.content, replacement=content, observed=observed),
                    )
                    # An earlier positive read may have compiled this SELECT.
                    # Recompile against the explicit test-only result processor.
                    conn.execution_options(compiled_cache=None)
                    raw_content = conn.execute(
                        text("SELECT content FROM chat_messages WHERE id = :audit_message_id"), {"audit_message_id": row.id}
                    ).scalar_one()
                    assert raw_content == row.content
                    with pytest.raises(AuditIntegrityError):
                        read_tool_failure_source_on_connection(conn, running, env_ref_names=owner.runtime.env_ref_names)
                    assert observed and all(value == row.content for value in observed)
                savepoint.rollback()
            assert read_tool_failure_source_on_connection(conn, running, env_ref_names=owner.runtime.env_ref_names) is not None
            unchanged = conn.execute(select(messages).where(messages.c.id == row.id)).one()
            assert unchanged.tool_calls == row.tool_calls and unchanged.content == row.content
        assert len(sdk) == 1
