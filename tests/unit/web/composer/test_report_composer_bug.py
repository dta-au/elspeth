"""``report_composer_bug``: a suspected guidance/tool mismatch becomes one bounded session file.

Behaviour pinned here:

* the report is an ordinary assistant-created session blob with the composer
  context that produced it, marked suspected and unverified;
* the pipeline state is returned unchanged;
* every free-text field is capped in characters, and the saved UTF-8 file in
  bytes (exactly at the byte limit is admitted, one byte over is refused); an
  over-size or malformed report is refused as a model-readable
  ``ToolArgumentError`` and writes nothing;
* a session keeps at most a fixed number of reports; past it the call fails
  with a model-readable message and writes nothing;
* the report prose is redacted from the assistant row's tool-call record (the
  tool and audit rows keep the arguments, as for every tool);
* the report runs immediately under explicit approval, like ``create_blob``,
  while destructive blob writes still require approval.

The symbols this change adds are looked up when a test runs (``_impl``), not
at import, so the file collects on a tree without the tool and fails there by
assertion.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer import protocol, redaction
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.tools import get_tool_definitions
from elspeth.web.composer.tools._registry import _REGISTERED_TOOLS
from elspeth.web.composer.tools.discovery import (
    is_approval_required_blob_store_only_mutation_tool,
    is_blob_store_only_mutation_tool,
)
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions.models import blobs_table, sessions_table
from tests.unit.web.composer.test_tools import (
    _aws_s3_source_state,
    _blob_operation,
    _empty_state,
    _insert_user_message,
    _mock_catalog,
    _session_engine_with_session,
    _trained_tool_context,
    execute_tool,
)

_REPORTS_MODULE = "elspeth.web.composer.tools.reports"
_SKILL_HASH = "a" * 64
_ARGUMENTS_HASH = "b" * 64


@dataclass(frozen=True)
class _Impl:
    """The report tool's own symbols, resolved at call time."""

    reports: ModuleType
    model: type[BaseModel]
    max_bytes: int
    too_large_expectation: str
    too_large_actual: str
    caps: dict[str, int]


def _impl() -> _Impl:
    assert importlib.util.find_spec(_REPORTS_MODULE) is not None, "the report_composer_bug tool module does not exist"
    protocol_names = vars(protocol)
    redaction_names = vars(redaction)
    for name in ("COMPOSER_BUG_REPORT_MAX_BYTES", "COMPOSER_BUG_REPORT_TOO_LARGE_EXPECTATION", "COMPOSER_BUG_REPORT_TOO_LARGE_ACTUAL_TYPE"):
        assert name in protocol_names, f"protocol.{name} is missing"
    for name in (
        "ReportComposerBugArgumentsModel",
        "REPORT_SUMMARY_MAX_CHARS",
        "REPORT_BEHAVIOR_MAX_CHARS",
        "REPORT_GUIDANCE_REFERENCE_MAX_CHARS",
        "REPORT_RELATED_TOOL_MAX_CHARS",
    ):
        assert name in redaction_names, f"redaction.{name} is missing"
    behavior_cap = redaction_names["REPORT_BEHAVIOR_MAX_CHARS"]
    return _Impl(
        reports=importlib.import_module(_REPORTS_MODULE),
        model=redaction_names["ReportComposerBugArgumentsModel"],
        max_bytes=protocol_names["COMPOSER_BUG_REPORT_MAX_BYTES"],
        too_large_expectation=protocol_names["COMPOSER_BUG_REPORT_TOO_LARGE_EXPECTATION"],
        too_large_actual=protocol_names["COMPOSER_BUG_REPORT_TOO_LARGE_ACTUAL_TYPE"],
        caps={
            "summary": redaction_names["REPORT_SUMMARY_MAX_CHARS"],
            "expected_behavior": behavior_cap,
            "observed_behavior": behavior_cap,
            "guidance_reference": redaction_names["REPORT_GUIDANCE_REFERENCE_MAX_CHARS"],
            "related_tool": redaction_names["REPORT_RELATED_TOOL_MAX_CHARS"],
        },
    )


def _arguments() -> dict[str, str]:
    return {
        "summary": "File binding disagrees with the skill",
        "expected_behavior": "The skill says a query template can use a text file.",
        "observed_behavior": "wire_blob_inline_ref rejected the query template path.",
        "guidance_reference": "pipeline_composer.md / File-backed prompts",
        "related_tool": "wire_blob_inline_ref",
    }


def _max_ascii_arguments(impl: _Impl) -> dict[str, str]:
    """Every field at its cap, in plain ASCII: the largest ordinary report, which must fit."""
    return {field: field[0] * cap for field, cap in impl.caps.items()}


def _oversize_file_arguments(impl: _Impl) -> dict[str, str]:
    """Within every field cap, but three-byte text in both behaviour fields makes the file too large."""
    behavior_cap = impl.caps["observed_behavior"]
    return {**_arguments(), "expected_behavior": "漢" * behavior_cap, "observed_behavior": "漢" * behavior_cap}


def _blob_rows(engine: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(select(blobs_table)).all())


@contextmanager
def _compose_operation(engine: Any, session_id: str, tmp_path: Path) -> Iterator[dict[str, Any]]:
    """A user message and a compose operation fence in ``session_id``, as the compose loop supplies them."""
    message_id = _insert_user_message(engine, session_id, "Reconnect the scoring prompt file.")
    with _blob_operation(engine, session_id) as (authority, operation):
        yield {
            "data_dir": str(tmp_path),
            "session_engine": engine,
            "session_id": session_id,
            "session_operation_authority": authority,
            "session_operation_context": operation,
            "user_message_id": message_id,
            "composer_model_identifier": "openai/sonnet",
            "composer_model_version": "synthetic-test-model",
            "composer_provider": "test-provider",
            "composer_skill_hash": _SKILL_HASH,
            "tool_arguments_hash": _ARGUMENTS_HASH,
        }


@contextmanager
def _report_session(tmp_path: Path, *, prepare: Callable[[Any, str], None] | None = None) -> Iterator[tuple[Any, dict[str, Any]]]:
    """A real session in a compose operation; ``prepare`` runs first, outside that operation."""
    engine, session_id = _session_engine_with_session()
    try:
        if prepare is not None:
            prepare(engine, session_id)
        with _compose_operation(engine, session_id, tmp_path) as kwargs:
            yield engine, kwargs
    finally:
        engine.dispose()


def _add_session(engine: Any) -> str:
    """A second session for the same user in the same store."""
    session_id = str(uuid4())
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            sessions_table.insert().values(
                id=session_id,
                user_id="test-user",
                auth_provider_type="local",
                title="Other Session",
                created_at=now,
                updated_at=now,
            )
        )
    return session_id


def _session_report_rows(engine: Any, session_id: str, description: str) -> list[Any]:
    return [row for row in _blob_rows(engine) if row.session_id == session_id and row.source_description == description]


def test_report_persists_one_bounded_session_file_and_leaves_the_pipeline_unchanged(tmp_path: Path) -> None:
    impl = _impl()
    state = _aws_s3_source_state()
    assert state.sources
    with _report_session(tmp_path) as (engine, kwargs):
        result = execute_tool("report_composer_bug", _arguments(), state, _mock_catalog(), **kwargs)
        rows = _blob_rows(engine)

    assert result.success
    assert result.updated_state is state
    assert len(rows) == 1
    row = rows[0]
    assert row.id == result.data["blob_id"]
    assert row.session_id == kwargs["session_id"]
    assert row.created_by == "assistant"
    assert row.mime_type == "application/json"
    assert row.created_from_message_id == kwargs["user_message_id"]
    assert row.creating_composer_skill_hash == _SKILL_HASH
    assert row.creating_arguments_hash == _ARGUMENTS_HASH
    assert row.filename.startswith("composer-bug-report-") and row.filename.endswith(".json")

    saved = Path(row.storage_path).read_bytes()
    assert len(saved) <= impl.max_bytes
    report = json.loads(saved)
    assert report["report_type"] == impl.reports.REPORT_TYPE == "suspected_composer_bug"
    assert report["status"] == impl.reports.REPORT_STATUS == "suspected_unverified"
    assert report["observations"] == _arguments()
    assert report["context"] == {
        "session_id": kwargs["session_id"],
        "state_id": None,
        "state_version": state.version,
        "user_message_id": kwargs["user_message_id"],
        "model": "openai/sonnet",
        "model_version": "synthetic-test-model",
        "provider": "test-provider",
        "skill_hash": _SKILL_HASH,
        "tool_arguments_hash": _ARGUMENTS_HASH,
    }
    # The tool result names the file; it does not echo the report prose.
    assert result.data == {"blob_id": row.id, "filename": row.filename, "status": "suspected_unverified"}


def test_report_omits_optional_fields_that_were_not_supplied(tmp_path: Path) -> None:
    _impl()
    arguments = {key: _arguments()[key] for key in ("summary", "expected_behavior", "observed_behavior")}
    with _report_session(tmp_path) as (engine, kwargs):
        result = execute_tool("report_composer_bug", arguments, _empty_state(), _mock_catalog(), **kwargs)
        rows = _blob_rows(engine)

    assert result.success
    assert json.loads(Path(rows[0].storage_path).read_text())["observations"] == arguments


def test_report_at_every_field_cap_in_ascii_fits_the_file_limit(tmp_path: Path) -> None:
    """Exact-boundary control: the file cap never refuses a report the field caps admit in ASCII."""
    impl = _impl()
    with _report_session(tmp_path) as (engine, kwargs):
        result = execute_tool("report_composer_bug", _max_ascii_arguments(impl), _empty_state(), _mock_catalog(), **kwargs)
        rows = _blob_rows(engine)

    assert result.success
    assert len(rows) == 1
    assert len(Path(rows[0].storage_path).read_bytes()) <= impl.max_bytes


def test_report_over_the_file_limit_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    impl = _impl()
    arguments = _oversize_file_arguments(impl)
    impl.model.model_validate(arguments)  # every field is within its own cap
    with _report_session(tmp_path) as (engine, kwargs):
        with pytest.raises(ToolArgumentError) as refused:
            execute_tool("report_composer_bug", arguments, _empty_state(), _mock_catalog(), **kwargs)
        rows = _blob_rows(engine)

    assert rows == []
    assert list(tmp_path.rglob("*.json")) == []
    message = str(refused.value)
    assert impl.too_large_expectation in message
    assert impl.too_large_actual in message
    assert str(impl.max_bytes) in message


def test_file_limit_admits_exactly_the_limit_and_refuses_one_byte_more(tmp_path: Path) -> None:
    """Exact byte boundary: a file of exactly the limit is saved; one byte more is refused and writes nothing."""
    impl = _impl()
    render = vars(impl.reports)["_report_content"]
    state = _empty_state()
    behavior_cap = impl.caps["observed_behavior"]

    def required(expected_length: int) -> dict[str, str]:
        # Three-byte text fills most of the file; ASCII padding sets the exact size.
        return {"summary": "Exact boundary", "expected_behavior": "x" * expected_length, "observed_behavior": "漢" * behavior_cap}

    with _report_session(tmp_path) as (engine, kwargs):
        context = _trained_tool_context(**kwargs)
        unpadded = len(render(required(1), state, context).encode("utf-8"))
        at_limit = required(1 + impl.max_bytes - unpadded)
        over_limit = required(2 + impl.max_bytes - unpadded)
        impl.model.model_validate(at_limit)
        impl.model.model_validate(over_limit)
        # Control the instrument: the padding lands exactly on the limit and one byte past it.
        assert len(render(at_limit, state, context).encode("utf-8")) == impl.max_bytes
        assert len(render(over_limit, state, context).encode("utf-8")) == impl.max_bytes + 1

        with pytest.raises(ToolArgumentError):
            impl.reports._execute_report_composer_bug(arguments=over_limit, state=state, context=context)
        assert _blob_rows(engine) == []
        result = impl.reports._execute_report_composer_bug(arguments=at_limit, state=state, context=context)
        rows = _blob_rows(engine)

    assert result.success
    assert len(rows) == 1
    assert len(Path(rows[0].storage_path).read_bytes()) == impl.max_bytes


@pytest.mark.parametrize("character", ["é", "漢"], ids=["two_byte", "three_byte"])
def test_one_behaviour_field_at_its_cap_in_multibyte_text_fits_and_is_stored_as_utf8(tmp_path: Path, character: str) -> None:
    """The advertised character cap holds for non-ASCII text: the file stores UTF-8, not escapes."""
    impl = _impl()
    text = character * impl.caps["observed_behavior"]
    with _report_session(tmp_path) as (engine, kwargs):
        result = execute_tool("report_composer_bug", {**_arguments(), "observed_behavior": text}, _empty_state(), _mock_catalog(), **kwargs)
        rows = _blob_rows(engine)

    assert result.success
    saved = Path(rows[0].storage_path).read_bytes()
    assert len(saved) <= impl.max_bytes
    assert text.encode("utf-8") in saved
    assert json.loads(saved)["observations"]["observed_behavior"] == text


def test_session_report_limit_refuses_the_next_report_and_continues(tmp_path: Path) -> None:
    """At the per-session limit the call fails with a model-readable message and writes nothing.

    Only this tool's reports count: another assistant file with the same
    filename prefix does not use up the limit.
    """
    impl = _impl()
    reports = vars(impl.reports)
    assert "COMPOSER_BUG_REPORTS_PER_SESSION_MAX" in reports, "the per-session report limit is missing"
    limit = reports["COMPOSER_BUG_REPORTS_PER_SESSION_MAX"]
    assert limit == 20
    state = _empty_state()
    with _report_session(tmp_path) as (engine, kwargs):
        other = execute_tool(
            "create_blob",
            {"filename": "composer-bug-report-notes.json", "mime_type": "application/json", "content": "{}"},
            state,
            _mock_catalog(),
            **kwargs,
        )
        assert other.success
        for _ in range(limit):
            assert execute_tool("report_composer_bug", _arguments(), state, _mock_catalog(), **kwargs).success
        refused = execute_tool("report_composer_bug", _arguments(), state, _mock_catalog(), **kwargs)
        rows = _blob_rows(engine)

    assert not refused.success
    assert refused.updated_state is state
    message = refused.validation.errors[0].message
    assert f"{limit} composer bug reports" in message
    assert "not saved" in message
    assert "continue the user's task" in message
    assert len(rows) == limit + 1


def test_reports_in_another_session_do_not_use_up_this_sessions_limit(tmp_path: Path) -> None:
    """The count is per session: another session at the limit leaves this one free to report."""
    impl = _impl()
    limit = vars(impl.reports)["COMPOSER_BUG_REPORTS_PER_SESSION_MAX"]
    description = vars(impl.reports)["_REPORT_DESCRIPTION"]
    state = _empty_state()
    other: dict[str, str] = {}

    def fill_another_session(engine: Any, _session_id: str) -> None:
        other["session_id"] = _add_session(engine)
        with _compose_operation(engine, other["session_id"], tmp_path) as other_kwargs:
            for _ in range(limit):
                assert execute_tool("report_composer_bug", _arguments(), state, _mock_catalog(), **other_kwargs).success
            assert not execute_tool("report_composer_bug", _arguments(), state, _mock_catalog(), **other_kwargs).success

    with _report_session(tmp_path, prepare=fill_another_session) as (engine, kwargs):
        result = execute_tool("report_composer_bug", _arguments(), state, _mock_catalog(), **kwargs)
        assert result.success
        assert len(_session_report_rows(engine, other["session_id"], description)) == limit
        assert len(_session_report_rows(engine, kwargs["session_id"], description)) == 1


def test_user_uploaded_lookalike_file_does_not_use_up_the_limit(tmp_path: Path) -> None:
    """Only files the assistant created count: a user upload with the report's name prefix and description does not."""
    impl = _impl()
    limit = vars(impl.reports)["COMPOSER_BUG_REPORTS_PER_SESSION_MAX"]
    description = vars(impl.reports)["_REPORT_DESCRIPTION"]
    state = _empty_state()

    def upload_lookalike(engine: Any, session_id: str) -> None:
        with _blob_operation(engine, session_id, kind=SessionOperationKind.CREATE) as (_authority, create_context):
            record = asyncio.run(
                BlobServiceImpl(engine, tmp_path).create_blob(
                    session_id=UUID(session_id),
                    filename="composer-bug-report-uploaded.json",
                    content=b"{}",
                    mime_type="application/json",
                    created_by="user",
                    source_description=description,
                    session_operation_context=create_context,
                )
            )
        assert record.created_by == "user"

    with _report_session(tmp_path, prepare=upload_lookalike) as (engine, kwargs):
        for _ in range(limit):
            assert execute_tool("report_composer_bug", _arguments(), state, _mock_catalog(), **kwargs).success
        refused = execute_tool("report_composer_bug", _arguments(), state, _mock_catalog(), **kwargs)
        rows = _session_report_rows(engine, kwargs["session_id"], description)

    assert not refused.success
    assert sorted(row.created_by for row in rows) == ["assistant"] * limit + ["user"]


def test_deleting_a_report_removes_its_row_and_frees_one_slot(tmp_path: Path) -> None:
    """``delete_blob`` removes a report's row and file, so the session can report once more, up to the limit."""
    impl = _impl()
    limit = vars(impl.reports)["COMPOSER_BUG_REPORTS_PER_SESSION_MAX"]
    state = _empty_state()
    with _report_session(tmp_path) as (engine, kwargs):
        report_ids = [
            execute_tool("report_composer_bug", _arguments(), state, _mock_catalog(), **kwargs).data["blob_id"] for _ in range(limit)
        ]
        assert not execute_tool("report_composer_bug", _arguments(), state, _mock_catalog(), **kwargs).success
        deleted_path = Path(next(row.storage_path for row in _blob_rows(engine) if row.id == report_ids[0]))

        deleted = execute_tool("delete_blob", {"blob_id": report_ids[0]}, state, _mock_catalog(), **kwargs)
        assert deleted.success
        assert deleted.data == {"blob_id": report_ids[0], "deleted": True}
        remaining = {row.id for row in _blob_rows(engine)}
        assert report_ids[0] not in remaining
        assert len(remaining) == limit - 1
        assert not deleted_path.exists()

        assert execute_tool("report_composer_bug", _arguments(), state, _mock_catalog(), **kwargs).success
        assert not execute_tool("report_composer_bug", _arguments(), state, _mock_catalog(), **kwargs).success
        assert len(_blob_rows(engine)) == limit


def test_report_over_the_session_quota_fails_without_a_file(tmp_path: Path) -> None:
    _impl()
    with _report_session(tmp_path) as (engine, kwargs):
        result = execute_tool(
            "report_composer_bug",
            _arguments(),
            _empty_state(),
            _mock_catalog(),
            **{**kwargs, "max_blob_storage_per_session_bytes": 3},
        )
        rows = _blob_rows(engine)

    assert not result.success
    assert "3 byte limit" in result.validation.errors[0].message
    assert rows == []


def test_handler_refuses_malformed_or_oversize_arguments_without_writing(tmp_path: Path) -> None:
    """Trust-boundary honesty test for ``_execute_report_composer_bug``.

    Malformed arguments and a report over the file limit both raise the
    model-readable ToolArgumentError before any file or row is written.
    """
    impl = _impl()
    engine, session_id = _session_engine_with_session()
    message_id = _insert_user_message(engine, session_id, "Reconnect the scoring prompt file.")
    try:
        with _blob_operation(engine, session_id) as (authority, operation):
            context = _trained_tool_context(
                data_dir=str(tmp_path),
                session_engine=engine,
                session_id=session_id,
                session_operation_authority=authority,
                session_operation_context=operation,
                user_message_id=message_id,
                composer_model_identifier="openai/sonnet",
                composer_model_version="synthetic-test-model",
                composer_provider="test-provider",
                composer_skill_hash=_SKILL_HASH,
                tool_arguments_hash=_ARGUMENTS_HASH,
            )
            with pytest.raises(ToolArgumentError, match="report_composer_bug arguments"):
                impl.reports._execute_report_composer_bug(arguments={**_arguments(), "summary": 42}, state=_empty_state(), context=context)
            with pytest.raises(ToolArgumentError, match="report_composer_bug arguments"):
                impl.reports._execute_report_composer_bug(arguments=_oversize_file_arguments(impl), state=_empty_state(), context=context)
        with engine.connect() as conn:
            assert conn.execute(select(blobs_table)).all() == []
        assert list(tmp_path.rglob("*")) == []
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("field", "kind"),
    [
        ("summary", "over_cap"),
        ("expected_behavior", "over_cap"),
        ("observed_behavior", "over_cap"),
        ("guidance_reference", "over_cap"),
        ("related_tool", "over_cap"),
        ("observed_behavior", "empty"),
        ("observed_behavior", "blank"),
        ("summary", "not_text"),
        ("guidance_reference", "null"),
        ("related_tool", "not_a_tool_name"),
        ("unexpected", "extra_key"),
    ],
)
def test_model_refuses_oversize_blank_mistyped_null_or_extra_fields(field: str, kind: str) -> None:
    impl = _impl()
    values: dict[str, object] = {
        "over_cap": "t" * (impl.caps[field] + 1) if field in impl.caps else None,
        "empty": "",
        "blank": " \n\t ",
        "not_text": 42,
        "null": None,
        "not_a_tool_name": "Not A Tool",
        "extra_key": "extra key",
    }
    with pytest.raises(ValidationError):
        impl.model.model_validate({**_arguments(), field: values[kind]})


def test_model_admits_every_field_exactly_at_its_cap() -> None:
    """+1 above is refused; exactly at the cap is admitted."""
    impl = _impl()
    admitted = impl.model.model_validate(_max_ascii_arguments(impl))
    assert admitted.model_dump() == _max_ascii_arguments(impl)


def test_malformed_arguments_reach_the_model_as_a_tool_argument_error() -> None:
    _impl()
    with pytest.raises(ToolArgumentError, match="report_composer_bug arguments"):
        execute_tool("report_composer_bug", {**_arguments(), "summary": ""}, _empty_state(), _mock_catalog())


def test_declared_schema_bounds_match_the_admitting_model() -> None:
    impl = _impl()
    declarations = [decl for decl in _REGISTERED_TOOLS if decl.name == "report_composer_bug"]
    assert len(declarations) == 1
    properties = declarations[0].json_schema["properties"]
    model_schema = impl.model.model_json_schema()["properties"]
    for field, cap in impl.caps.items():
        assert properties[field]["maxLength"] == model_schema[field]["maxLength"] == cap, field
    assert list(declarations[0].json_schema["required"]) == ["summary", "expected_behavior", "observed_behavior"]
    assert declarations[0].json_schema["additionalProperties"] is False


def test_report_is_offered_and_runs_without_approval_while_destructive_blob_writes_still_need_it() -> None:
    assert any(tool["name"] == "report_composer_bug" for tool in get_tool_definitions())
    assert is_blob_store_only_mutation_tool("report_composer_bug")
    assert not is_approval_required_blob_store_only_mutation_tool("report_composer_bug")
    assert not is_approval_required_blob_store_only_mutation_tool("create_blob")
    assert is_approval_required_blob_store_only_mutation_tool("update_blob")
    assert is_approval_required_blob_store_only_mutation_tool("delete_blob")


def test_report_prose_is_redacted_from_chat_tool_arguments() -> None:
    _impl()
    args = _arguments()
    redacted = redact_tool_call_arguments("report_composer_bug", args, telemetry=NoopRedactionTelemetry())
    for field in ("summary", "expected_behavior", "observed_behavior", "guidance_reference"):
        assert redacted[field] != args[field]
        assert args[field] not in json.dumps(redacted)
    assert redacted["related_tool"] == "wire_blob_inline_ref"


def test_report_refuses_credentials_before_persisting_any_artifact(tmp_path: Path) -> None:
    _impl()
    credential = "sk-" + "x" * 64
    with _report_session(tmp_path) as (engine, kwargs):
        with pytest.raises(CredentialMaterialRefused):
            execute_tool(
                "report_composer_bug",
                {**_arguments(), "observed_behavior": f"Authorization: Bearer {credential}"},
                _empty_state(),
                _mock_catalog(),
                **kwargs,
            )
        rows = _blob_rows(engine)

    assert rows == []
    assert list(tmp_path.rglob("*.json")) == []
