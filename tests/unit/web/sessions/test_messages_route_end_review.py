"""END advisor-checkpoint pass rows in the LLM audit view of GET /messages.

The view admits stored ``advisor_checkpoint_pass_audit`` rows whose phase is
``end`` under ``include_llm_audit=true``, cut down to six fields. Rows that do
not parse are left out and counted in ``X-Elspeth-End-Review-Rows-Omitted``.
"""

from __future__ import annotations

import ast
import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi.middleware.cors import CORSMiddleware

from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.advisor_audit import (
    ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
    AdvisorCheckpointPassRecord,
    advisor_checkpoint_pass_audit_envelope,
    persist_advisor_checkpoint_pass,
)
from elspeth.web.composer.advisor_checkpoint_telemetry import (
    AdvisorCheckpointTelemetryVerdict,
    AdvisorCheckpointVerdictSource,
)
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory
from elspeth.web.sessions.models import identity_roles_table
from elspeth.web.sessions.routes._helpers import _composer_chat_history
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.tree_gate import iter_gate_files
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness
from tests.unit.web.sessions.test_routes import _llm_call, _llm_call_audit_tool_calls, _make_app

# Spelled out, not imported, so this file collects on a tree without the view.
END_REVIEW_ROWS_OMITTED_HEADER = "X-Elspeth-End-Review-Rows-Omitted"
END_REVIEW_FIELDS = ("phase", "pass_index", "verdict", "source", "provider_attempts", "publication_verdict")
FINDINGS_CANARY = "FINDINGS_CANARY_5d1e"
REQUEST = "Build the pipeline"
_SRC = Path(__file__).resolve().parents[4] / "src" / "elspeth"


def _end_record() -> AdvisorCheckpointPassRecord:
    return AdvisorCheckpointPassRecord.from_findings(
        phase="end",
        pass_index=1,
        verdict="flagged",
        source="model",
        findings_text=f"FLAGGED: {FINDINGS_CANARY}",
        provider_attempts=1,
        first_attempt_schema_valid=True,
        first_attempt_accepted=True,
        format_reprompt_sent=False,
        step_ids_offered=3,
        step_ids_kept=1,
        note_present=True,
        url_redactions=2,
        email_redactions=1,
        publication_verdict="WITHHOLD",
        publication_binding=stable_hash({"candidate": "synthetic", "evidence": "synthetic"}),
    )


def _early_record() -> AdvisorCheckpointPassRecord:
    return AdvisorCheckpointPassRecord.from_findings(
        phase="early",
        pass_index=1,
        verdict="clean",
        source="model",
        findings_text="CLEAN",
        provider_attempts=1,
        first_attempt_schema_valid=True,
        first_attempt_accepted=True,
        format_reprompt_sent=False,
        step_ids_offered=0,
        step_ids_kept=0,
        note_present=False,
        url_redactions=0,
        email_redactions=0,
        publication_verdict=None,
        publication_binding=None,
    )


def _stored_pass(**overrides: Any) -> dict[str, Any]:
    """A stored END pass payload as the writer serialises it, with overrides."""
    payload: dict[str, Any] = dict(_end_record().to_dict())
    payload.update(overrides)
    return payload


async def _add_audit_rows(service: FencedSessionServiceHarness, session_id: uuid.UUID, rows: list[list[dict[str, Any]]]) -> None:
    """Store raw ``role="audit"`` rows, one per ``tool_calls`` list, as the compose loop writes them."""
    async with service._call_context(session_id, SessionOperationKind.COMPOSE) as context:
        for tool_calls in rows:
            await service.add_message(
                session_id,
                "audit",
                json.dumps(tool_calls[0], sort_keys=True),
                tool_calls=tool_calls,
                writer_principal="compose_loop",
                session_operation_context=context,
            )


def _session_with_rows(tmp_path: Path) -> tuple[SyncASGITestClient, FencedSessionServiceHarness, Any, uuid.UUID]:
    app, built = _make_app(tmp_path)
    assert type(built) is FencedSessionServiceHarness
    service = built
    client = SyncASGITestClient(app, raise_server_exceptions=False)
    created = client.post("/api/sessions", json={"title": "End review"})
    assert created.status_code == 201, created.text
    session_id = uuid.UUID(created.json()["id"])

    async def seed() -> None:
        async with service._call_context(session_id, SessionOperationKind.COMPOSE) as context:
            await service.add_message(session_id, "user", REQUEST, writer_principal="route_user_message", session_operation_context=context)
        await _add_audit_rows(service, session_id, [_llm_call_audit_tool_calls(_llm_call(provider_request_id="chatcmpl-end"))])
        async with service._call_context(session_id, SessionOperationKind.COMPOSE) as context:
            # The real writer, as the advisor checkpoint calls it.
            for record in (_early_record(), _end_record()):
                await persist_advisor_checkpoint_pass(
                    sessions=service, session_id=str(session_id), session_operation_context=context, record=record
                )
            await service.add_message(session_id, "assistant", "Done.", writer_principal="compose_loop", session_operation_context=context)

    asyncio.run(seed())
    return client, service, app, session_id


def _end_rows(body: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        message
        for message in body
        if message["tool_calls"] is not None and message["tool_calls"][0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    ]


class TestEndRowsInTheLlmAuditView:
    def test_end_row_is_returned_with_exactly_the_six_fields(self, tmp_path: Path) -> None:
        client, _service, _app, session_id = _session_with_rows(tmp_path)

        response = client.get(f"/api/sessions/{session_id}/messages?include_llm_audit=true")

        assert response.status_code == 200, response.text
        body = response.json()
        assert [message["role"] for message in body] == ["user", "audit", "audit", "assistant"]
        rows = _end_rows(body)
        assert len(rows) == 1
        row = rows[0]
        assert row["tool_calls"] == [
            {
                "_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
                "pass": {
                    "phase": "end",
                    "pass_index": 1,
                    "verdict": "flagged",
                    "source": "model",
                    "provider_attempts": 1,
                    "publication_verdict": "WITHHOLD",
                },
            }
        ]
        assert json.loads(row["content"]) == row["tool_calls"][0]
        assert row["raw_content"] is None
        stored = _end_record()
        for private in (stored.findings_hash, stored.publication_binding, FINDINGS_CANARY, "findings_hash", "publication_binding"):
            assert private is not None
            assert private not in response.text
        assert response.headers[END_REVIEW_ROWS_OMITTED_HEADER] == "0"

    def test_end_rows_are_absent_without_include_llm_audit(self, tmp_path: Path) -> None:
        client, _service, _app, session_id = _session_with_rows(tmp_path)

        for query in (
            "",
            "?include_tool_rows=true",
            "?include_raw_content=true",
            "?include_tool_rows=true&include_rejection_reasons=true",
            "?include_llm_audit=false",
        ):
            response = client.get(f"/api/sessions/{session_id}/messages{query}")
            assert response.status_code == 200, response.text
            assert _end_rows(response.json()) == []
            assert ADVISOR_CHECKPOINT_PASS_AUDIT_KIND not in response.text
            assert END_REVIEW_ROWS_OMITTED_HEADER not in response.headers

    def test_end_rows_join_the_view_with_tool_rows(self, tmp_path: Path) -> None:
        client, _service, _app, session_id = _session_with_rows(tmp_path)

        response = client.get(f"/api/sessions/{session_id}/messages?include_llm_audit=true&include_tool_rows=true")

        assert response.status_code == 200, response.text
        assert len(_end_rows(response.json())) == 1
        assert response.headers[END_REVIEW_ROWS_OMITTED_HEADER] == "0"

    def test_header_reaches_a_cross_origin_client_unexposed_to_browser_script(self, tmp_path: Path) -> None:
        """The header is sent under the production CORS settings. They list no
        ``expose_headers``, so script in a browser page on another origin cannot
        read it; the battery is not a browser and reads it, and the SPA never
        asks for ``include_llm_audit``."""
        _client, _service, app, session_id = _session_with_rows(tmp_path)
        origin = "https://spa.example.test"
        cors = CORSMiddleware(app, allow_origins=[origin], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
        client = SyncASGITestClient(app, transport_app=cors)

        response = client.get(f"/api/sessions/{session_id}/messages?include_llm_audit=true", headers={"Origin": origin})

        assert response.status_code == 200, response.text
        assert response.headers["access-control-allow-origin"] == origin
        assert response.headers[END_REVIEW_ROWS_OMITTED_HEADER] == "0"
        assert "access-control-expose-headers" not in response.headers

    def test_pagination_counts_end_rows_in_sequence_order(self, tmp_path: Path) -> None:
        client, _service, _app, session_id = _session_with_rows(tmp_path)

        page = client.get(f"/api/sessions/{session_id}/messages?include_llm_audit=true&offset=2&limit=1")

        assert page.status_code == 200, page.text
        assert len(_end_rows(page.json())) == 1


class TestAuthorization:
    """Who may call the view: the session's owner holding the deployment ``user``
    role, exactly as for the existing ``include_llm_audit`` view; the call is
    recorded as an audit-grade access. Another user's session is refused."""

    def test_owner_call_is_recorded_as_an_audit_grade_view(self, tmp_path: Path) -> None:
        client, service, _app, session_id = _session_with_rows(tmp_path)

        response = client.get(f"/api/sessions/{session_id}/messages?include_llm_audit=true")

        assert response.status_code == 200
        access = service.list_audit_access_log(session_id=str(session_id))
        assert len(access) == 1
        assert access[0].requesting_principal == "alice"
        assert access[0].query_args == {"include_llm_audit": "true"}

    def test_another_users_session_is_refused(self, tmp_path: Path) -> None:
        client, service, app, session_id = _session_with_rows(tmp_path)
        with service._engine.begin() as conn:
            ensure_test_identity(conn, identity_id="bob")
            conn.execute(
                identity_roles_table.insert().values(
                    role_id=str(uuid.uuid4()),
                    identity_id="bob",
                    role="user",
                    scope=None,
                    granted_by_identity_id="bob",
                    granted_at=datetime.now(UTC),
                )
            )
        bob = UserIdentity(user_id="bob", username="bob")

        async def as_bob() -> UserIdentity:
            return bob

        app.dependency_overrides[get_current_user] = as_bob

        response = client.get(f"/api/sessions/{session_id}/messages?include_llm_audit=true")

        assert response.status_code == 404
        assert ADVISOR_CHECKPOINT_PASS_AUDIT_KIND not in response.text
        assert END_REVIEW_ROWS_OMITTED_HEADER not in response.headers
        assert service.list_audit_access_log(session_id=str(session_id)) == []


# Each stored payload below fails to parse to the view's shape and must be
# left out and counted, never served and never a 500.
_UNPARSEABLE: dict[str, list[dict[str, Any]]] = {
    "new_verdict_value": [{"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": _stored_pass(verdict="escalated")}],
    "new_source_value": [{"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": _stored_pass(source="cache")}],
    "new_publication_value": [{"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": _stored_pass(publication_verdict="MAYBE")}],
    "unknown_phase": [{"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": _stored_pass(phase="middle")}],
    "pass_index_bool": [{"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": _stored_pass(pass_index=True)}],
    "pass_index_negative": [{"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": _stored_pass(pass_index=-1)}],
    "pass_index_string": [{"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": _stored_pass(pass_index="1")}],
    "provider_attempts_three": [{"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": _stored_pass(provider_attempts=3)}],
    "provider_attempts_float": [{"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": _stored_pass(provider_attempts=1.0)}],
    "missing_field": [{"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": {k: v for k, v in _stored_pass().items() if k != "verdict"}}],
    "pass_not_a_mapping": [{"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": ["end", 1]}],
    "pass_missing": [{"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND}],
    "two_envelopes": [
        {"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": _stored_pass()},
        {"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": _stored_pass()},
    ],
    "beside_an_llm_call_audit": [
        _llm_call_audit_tool_calls(_llm_call(provider_request_id="chatcmpl-mixed"))[0],
        {"_kind": ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, "pass": _stored_pass()},
    ],
}


class TestUnparseableRowsAreOmittedAndCounted:
    @pytest.mark.parametrize("case", sorted(_UNPARSEABLE))
    def test_one_unparseable_row(self, tmp_path: Path, case: str) -> None:
        client, service, _app, session_id = _session_with_rows(tmp_path)
        asyncio.run(_add_audit_rows(service, session_id, [_UNPARSEABLE[case]]))

        response = client.get(f"/api/sessions/{session_id}/messages?include_llm_audit=true")

        assert response.status_code == 200, response.text
        assert response.headers[END_REVIEW_ROWS_OMITTED_HEADER] == "1"
        assert len(_end_rows(response.json())) == 1  # only the well-formed seeded row
        assert _end_record().findings_hash not in response.text
        assert "chatcmpl-mixed" not in response.text

    def test_every_unparseable_row_is_counted(self, tmp_path: Path) -> None:
        client, service, _app, session_id = _session_with_rows(tmp_path)

        asyncio.run(_add_audit_rows(service, session_id, list(_UNPARSEABLE.values())))

        response = client.get(f"/api/sessions/{session_id}/messages?include_llm_audit=true")

        assert response.status_code == 200, response.text
        assert response.headers[END_REVIEW_ROWS_OMITTED_HEADER] == str(len(_UNPARSEABLE))


class TestClosedValueSets:
    """Each view field's value set is closed. A value the stored writer may
    produce that the view does not list fails here, so it is reviewed before
    it can be served; a stored value outside the set is omitted and counted
    (``TestUnparseableRowsAreOmittedAndCounted``)."""

    def test_fields_are_exactly_the_six(self) -> None:
        from elspeth.web.sessions.routes import end_review_view
        from elspeth.web.sessions.schemas import AdvisorEndReviewPassResponse

        assert end_review_view.END_REVIEW_FIELDS == END_REVIEW_FIELDS
        assert end_review_view.END_REVIEW_ROWS_OMITTED_HEADER == END_REVIEW_ROWS_OMITTED_HEADER
        assert tuple(AdvisorEndReviewPassResponse.model_fields) == END_REVIEW_FIELDS

    def test_enum_value_sets(self) -> None:
        from elspeth.web.sessions.schemas import AdvisorEndReviewPassResponse

        fields = AdvisorEndReviewPassResponse.model_fields
        assert set(get_args(fields["phase"].annotation)) == {"end"}
        assert set(get_args(fields["verdict"].annotation)) == {"clean", "flagged", "unavailable", "request_rejected", "malformed"}
        assert set(get_args(fields["source"].annotation)) == {"prescan", "model"}
        publication, absent = get_args(fields["publication_verdict"].annotation)
        assert absent is type(None)
        assert set(get_args(publication)) == {"ADMIT", "WITHHOLD"}

    def test_the_parser_sets_are_the_model_sets(self) -> None:
        from elspeth.web.sessions.routes import end_review_view as view
        from elspeth.web.sessions.schemas import AdvisorEndReviewPassResponse

        fields = AdvisorEndReviewPassResponse.model_fields
        verdicts, sources = set(view.END_REVIEW_VERDICTS), set(view.END_REVIEW_SOURCES)
        publications, attempts = set(view.END_REVIEW_PUBLICATION_VERDICTS), set(view.END_REVIEW_PROVIDER_ATTEMPTS)
        assert verdicts == set(get_args(fields["verdict"].annotation))
        assert sources == set(get_args(fields["source"].annotation))
        assert publications == set(get_args(get_args(fields["publication_verdict"].annotation)[0]))
        assert attempts == {0, 1, 2}

    def test_enum_value_sets_match_the_stored_vocabulary(self) -> None:
        from elspeth.web.sessions.schemas import AdvisorEndReviewPassResponse

        fields = AdvisorEndReviewPassResponse.model_fields
        assert set(get_args(fields["verdict"].annotation)) == set(get_args(AdvisorCheckpointTelemetryVerdict))
        assert set(get_args(fields["source"].annotation)) == set(get_args(AdvisorCheckpointVerdictSource))

    @pytest.mark.parametrize(
        ("field", "accepted", "refused"),
        [
            ("pass_index", [0, 1, 7, 10_000], [-1, True, 1.0, "1", None]),
            ("provider_attempts", [0, 1, 2], [-1, 3, True, 1.0, "1", None]),
        ],
    )
    def test_integer_value_sets(self, field: str, accepted: list[object], refused: list[object]) -> None:
        from elspeth.web.sessions.schemas import AdvisorEndReviewPassResponse

        base = {name: _stored_pass()[name] for name in END_REVIEW_FIELDS}
        for value in accepted:
            assert AdvisorEndReviewPassResponse.model_validate({**base, field: value}).model_dump()[field] == value
        for value in refused:
            with pytest.raises(ValueError):
                AdvisorEndReviewPassResponse.model_validate({**base, field: value})


class TestRowsReachOnlyTheHttpResponse:
    """The stored pass rows never enter planner or advisor request assembly,
    and the cut-down projection is built only by the GET /messages route."""

    def test_planner_and_advisor_history_omit_pass_rows(self, tmp_path: Path) -> None:
        _client, service, _app, session_id = _session_with_rows(tmp_path)
        records = asyncio.run(service.get_messages(session_id, limit=None, include_private_evidence=False))
        pass_rows = [
            record
            for record in records
            if record.role == "audit"
            and record.tool_calls is not None
            and record.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
        ]
        assert len(pass_rows) == 2  # positive control: the rows are in the store
        stored_end = pass_rows[1].tool_calls
        assert stored_end is not None
        assert dict(stored_end[0]) == advisor_checkpoint_pass_audit_envelope(_end_record())
        user_row = next(record for record in records if record.role == "user")

        planner_history = _composer_chat_history(records)
        advisor_history = AdvisorTranscriptHistory.from_records(
            session_id=str(session_id),
            actor_user_id="alice",
            current_request_id=str(user_row.id),
            request=REQUEST,
            records=records,
        )

        assert planner_history == [
            {
                "role": "user",
                "content": REQUEST,
                "_elspeth_user_authored": True,
                "_elspeth_user_message_id": str(user_row.id),
            },
            {"role": "assistant", "content": "Done."},
        ]
        planner_text = json.dumps(planner_history)
        stored = _end_record()
        for marker in (
            ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
            stored.findings_hash,
            stored.publication_binding,
            *(str(r.id) for r in pass_rows),
        ):
            assert marker is not None
            assert marker not in planner_text
            assert marker not in advisor_history.events_json

    def test_only_the_messages_route_builds_the_projection(self) -> None:
        importers: set[str] = set()
        for path in iter_gate_files(_SRC):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                imports_view = (
                    isinstance(node, ast.ImportFrom) and node.module is not None and node.module.endswith("end_review_view")
                ) or (isinstance(node, ast.Import) and any(alias.name.endswith("end_review_view") for alias in node.names))
                if imports_view:
                    importers.add(path.relative_to(_SRC).as_posix())
        assert importers == {"web/sessions/routes/messages.py"}
