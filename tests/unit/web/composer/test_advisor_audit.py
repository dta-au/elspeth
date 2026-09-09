"""Advisor checkpoint events are audit rows first, telemetry second.

elspeth-fa18d54eef was diagnosed from two structlog events —
``composer.advisor_checkpoint_pass`` and
``composer.advisor_terminal_publication`` — which made the journal the only
record of which advisor verdicts a turn saw and which branch published its
fixed copy. The logging policy reserves logs for last resort: anything an
auditor would cite belongs in the audit trail first, synchronously, with
telemetry as the real-time mirror. ``web/composer/advisor_audit.py`` is that
seam; these tests pin its ordering, its refusals, and the row shape.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer import advisor_audit
from elspeth.web.composer.advisor_audit import (
    ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
    ADVISOR_TERMINAL_PUBLICATION_AUDIT_KIND,
    AdvisorCheckpointPassRecord,
    AdvisorTerminalPublication,
    advisor_checkpoint_pass_audit_envelope,
    advisor_terminal_publication_audit_envelope,
    persist_advisor_checkpoint_pass,
    persist_advisor_terminal_publication,
)
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.sessions.protocol import SessionServiceProtocol


def _context(session_id: str) -> SessionOperationContext:
    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=session_id,
            operation_id=f"advisor-audit-{session_id}",
            lease_token=f"advisor-audit-token-{session_id}",
            operation_epoch=1,
        ),
        operation_kind=SessionOperationKind.COMPOSE,
    )


def _pass_record() -> AdvisorCheckpointPassRecord:
    return AdvisorCheckpointPassRecord.from_findings(
        phase="end", pass_index=1, verdict="flagged", source="model", findings_text="FLAGGED: FINDINGS_CANARY"
    )


def _publication() -> AdvisorTerminalPublication:
    return AdvisorTerminalPublication(
        branch="repair_review", reason=None, preflight_shape="pending_handoff", findings_backend_authored=False
    )


class _OrderedSink:
    """Records audit writes and telemetry emits in the order they happen."""

    def __init__(self, *, write_failure: Exception | None = None) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._write_failure = write_failure
        self.sessions = MagicMock(spec=SessionServiceProtocol)
        self.sessions.add_message = self._add_message

    async def _add_message(self, session_id: Any, role: str, content: str, **kwargs: Any) -> None:
        if self._write_failure is not None:
            raise self._write_failure
        self.events.append(("audit", {"session_id": session_id, "role": role, "content": content, **kwargs}))

    def telemetry(self, **kwargs: Any) -> None:
        self.events.append(("telemetry", kwargs))


class TestRecords:
    def test_checkpoint_pass_hash_payload_shape_is_unchanged(self) -> None:
        """The journal has carried ``{"advisor_findings": text}`` since the
        event was introduced; the row must hash the same payload."""
        record = _pass_record()
        assert record.findings_hash == stable_hash({"advisor_findings": "FLAGGED: FINDINGS_CANARY"})
        assert "FINDINGS_CANARY" not in json.dumps(record.to_dict())

    def test_checkpoint_pass_canonicalization_refusal_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``stable_hash`` is first-party: a refusal is a programmer error and
        escapes before any row is written or event emitted."""

        def _refusing_hash(payload: Any) -> str:
            raise TypeError("canonicalization refusal")

        monkeypatch.setattr(advisor_audit, "stable_hash", _refusing_hash)
        with pytest.raises(TypeError, match="canonicalization refusal"):
            _pass_record()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"phase": "middle"},
            {"pass_index": -1},
            {"pass_index": True},
            {"verdict": "ok"},
            {"source": "human"},
            {"findings_hash": ""},
        ],
    )
    def test_checkpoint_pass_rejects_values_outside_the_closed_vocabulary(self, kwargs: dict[str, Any]) -> None:
        base: dict[str, Any] = {"phase": "end", "pass_index": 1, "verdict": "clean", "source": "model", "findings_hash": "h"}
        with pytest.raises(AuditIntegrityError):
            AdvisorCheckpointPassRecord(**{**base, **kwargs})

    @pytest.mark.parametrize(
        ("branch", "reason", "shape", "backend"),
        [
            ("nowhere", None, "green", False),
            ("terminal_block", None, "green", False),
            ("repair_review", "malformed", "green", False),
            ("terminal_block", "because", "green", False),
            ("repair_review", None, "purple", False),
            ("repair_review", None, "green", True),
        ],
        ids=["branch", "block_without_reason", "reason_off_block", "reason_vocab", "shape", "backend_finding_off_block"],
    )
    def test_publication_rejects_inconsistent_records(self, branch: str, reason: str | None, shape: str, backend: bool) -> None:
        with pytest.raises(AuditIntegrityError):
            AdvisorTerminalPublication(branch=branch, reason=reason, preflight_shape=shape, findings_backend_authored=backend)  # type: ignore[arg-type]

    def test_envelopes_carry_distinct_kinds_and_nothing_but_closed_fields(self) -> None:
        pass_envelope = advisor_checkpoint_pass_audit_envelope(_pass_record())
        publication_envelope = advisor_terminal_publication_audit_envelope(
            AdvisorTerminalPublication(
                branch="terminal_block", reason="flagged_final_pass", preflight_shape="absent", findings_backend_authored=True
            )
        )
        assert pass_envelope == {
            "_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
            "pass": {
                "phase": "end",
                "pass_index": 1,
                "verdict": "flagged",
                "source": "model",
                "findings_hash": stable_hash({"advisor_findings": "FLAGGED: FINDINGS_CANARY"}),
            },
        }
        assert publication_envelope == {
            "_kind": ADVISOR_TERMINAL_PUBLICATION_AUDIT_KIND,
            "publication": {
                "branch": "terminal_block",
                "reason": "flagged_final_pass",
                "preflight_shape": "absent",
                "findings_backend_authored": True,
            },
        }
        assert ADVISOR_CHECKPOINT_PASS_AUDIT_KIND != ADVISOR_TERMINAL_PUBLICATION_AUDIT_KIND


class TestAuditPrimacy:
    @pytest.mark.asyncio
    async def test_checkpoint_pass_row_commits_before_its_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sink = _OrderedSink()
        monkeypatch.setattr(advisor_audit, "record_advisor_checkpoint_pass", sink.telemetry)
        session_id = str(uuid.uuid4())
        record = _pass_record()

        await persist_advisor_checkpoint_pass(
            sessions=sink.sessions, session_id=session_id, session_operation_context=_context(session_id), record=record
        )

        envelope = advisor_checkpoint_pass_audit_envelope(record)
        assert [kind for kind, _ in sink.events] == ["audit", "telemetry"]
        row = sink.events[0][1]
        assert row["session_id"] == uuid.UUID(session_id)
        assert row["role"] == "audit"
        assert row["writer_principal"] == "compose_loop"
        assert row["tool_calls"] == [envelope]
        assert json.loads(row["content"]) == envelope
        assert row["session_operation_context"].fence.session_id == session_id
        assert sink.events[1][1] == {"session_id": session_id, **record.to_dict()}

    @pytest.mark.asyncio
    async def test_terminal_publication_row_commits_before_its_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sink = _OrderedSink()
        monkeypatch.setattr(advisor_audit, "record_advisor_terminal_publication", sink.telemetry)
        session_id = str(uuid.uuid4())
        publication = _publication()

        await persist_advisor_terminal_publication(
            sessions=sink.sessions, session_id=session_id, session_operation_context=_context(session_id), publication=publication
        )

        envelope = advisor_terminal_publication_audit_envelope(publication)
        assert [kind for kind, _ in sink.events] == ["audit", "telemetry"]
        row = sink.events[0][1]
        assert row["role"] == "audit"
        assert row["tool_calls"] == [envelope]
        assert json.loads(row["content"]) == envelope
        assert sink.events[1][1] == {"session_id": session_id, **publication.to_dict()}

    @pytest.mark.asyncio
    async def test_audit_write_failure_propagates_and_no_event_fires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A journal line claiming a pass the legal record does not hold is
        the inversion the policy forbids."""
        sink = _OrderedSink(write_failure=RuntimeError("session store down"))
        monkeypatch.setattr(advisor_audit, "record_advisor_checkpoint_pass", sink.telemetry)
        monkeypatch.setattr(advisor_audit, "record_advisor_terminal_publication", sink.telemetry)
        session_id = str(uuid.uuid4())

        with pytest.raises(RuntimeError, match="session store down"):
            await persist_advisor_checkpoint_pass(
                sessions=sink.sessions, session_id=session_id, session_operation_context=_context(session_id), record=_pass_record()
            )
        with pytest.raises(RuntimeError, match="session store down"):
            await persist_advisor_terminal_publication(
                sessions=sink.sessions, session_id=session_id, session_operation_context=_context(session_id), publication=_publication()
            )
        assert sink.events == []

    @pytest.mark.asyncio
    async def test_sessionless_compose_records_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No session means no session audit store (eval harnesses, direct
        unit invocation): there is no row, so no mirror event fires either —
        every path into the emitters has a committed row behind it."""
        sink = _OrderedSink()
        monkeypatch.setattr(advisor_audit, "record_advisor_checkpoint_pass", sink.telemetry)
        monkeypatch.setattr(advisor_audit, "record_advisor_terminal_publication", sink.telemetry)

        await persist_advisor_checkpoint_pass(sessions=None, session_id=None, session_operation_context=None, record=_pass_record())
        await persist_advisor_terminal_publication(
            sessions=None, session_id=None, session_operation_context=None, publication=_publication()
        )

        assert sink.events == []

    @pytest.mark.asyncio
    async def test_session_without_its_operation_context_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A session write is fenced; a session id without the turn's operation
        is a defect, never an unfenced write and never a silent downgrade."""
        sink = _OrderedSink()
        monkeypatch.setattr(advisor_audit, "record_advisor_checkpoint_pass", sink.telemetry)
        monkeypatch.setattr(advisor_audit, "record_advisor_terminal_publication", sink.telemetry)
        session_id = str(uuid.uuid4())

        with pytest.raises(TypeError, match="advisor_checkpoint_pass_audit row requires the turn's session_operation_context"):
            await persist_advisor_checkpoint_pass(
                sessions=sink.sessions, session_id=session_id, session_operation_context=None, record=_pass_record()
            )
        with pytest.raises(TypeError, match="advisor_terminal_publication_audit row requires the turn's session_operation_context"):
            await persist_advisor_terminal_publication(
                sessions=sink.sessions, session_id=session_id, session_operation_context=None, publication=_publication()
            )
        assert sink.events == []

    @pytest.mark.asyncio
    async def test_session_without_a_wired_store_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sink = _OrderedSink()
        monkeypatch.setattr(advisor_audit, "record_advisor_checkpoint_pass", sink.telemetry)
        session_id = str(uuid.uuid4())
        with pytest.raises(RuntimeError, match="sessions_service not wired"):
            await persist_advisor_checkpoint_pass(
                sessions=None, session_id=session_id, session_operation_context=_context(session_id), record=_pass_record()
            )
        assert sink.events == []


class TestRowsInARealSessionStore:
    @pytest.mark.asyncio
    async def test_rows_persist_as_audit_only_and_stay_out_of_the_conversation(self, tmp_path: Any) -> None:
        """Through the live fence into SQLite: both rows land as ``role="audit"``
        with their envelopes, are excluded from the LLM conversation, and are
        not mistaken for LLM-call sidecars by the opt-in audit view."""
        from elspeth.web.sessions.routes._helpers import (
            _composer_conversation_messages,
            _composer_conversation_or_llm_audit_messages,
        )

        from .conftest import build_test_sessions_service

        sessions = build_test_sessions_service(data_dir=tmp_path)
        session = await sessions.create_session("audit-user", "Advisor audit rows", "local")
        record = _pass_record()
        publication = _publication()

        async with sessions._call_context(session.id, SessionOperationKind.COMPOSE) as compose_context:
            await persist_advisor_checkpoint_pass(
                sessions=sessions, session_id=str(session.id), session_operation_context=compose_context, record=record
            )
            await persist_advisor_terminal_publication(
                sessions=sessions, session_id=str(session.id), session_operation_context=compose_context, publication=publication
            )

        messages = await sessions.get_messages(session.id)
        audit_rows = [message for message in messages if message.role == "audit"]
        assert [message.tool_calls[0]["_kind"] for message in audit_rows] == [
            ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
            ADVISOR_TERMINAL_PUBLICATION_AUDIT_KIND,
        ]
        assert [dict(message.tool_calls[0]) for message in audit_rows] == [
            advisor_checkpoint_pass_audit_envelope(record),
            advisor_terminal_publication_audit_envelope(publication),
        ]
        assert all(message.writer_principal == "compose_loop" for message in audit_rows)
        assert all(json.loads(message.content) == dict(message.tool_calls[0]) for message in audit_rows)
        assert _composer_conversation_messages(messages) == []
        assert _composer_conversation_or_llm_audit_messages(messages) == []
