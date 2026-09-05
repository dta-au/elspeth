"""ARG_ERROR routing + redaction for ``set_source_from_blob`` (Task 13 / Wave 2).

Final sub-task of Wave 2.  Same discipline as
``test_promote_create_blob.py`` and ``test_promote_update_blob.py``
(rev-2 BLOCKER_A).  ``set_source_from_blob`` shares the
:func:`_summarize_set_source_options` summarizer with
:class:`SetSourceArgumentsModel` (Task 4); both source-binding tools
must apply uniform redaction to caller-supplied ``options`` dicts so an
LLM that happens to include a path-like field receives the same
discipline regardless of which binding tool it invoked.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import insert
from sqlalchemy.pool import StaticPool

from elspeth.contracts.enums import CreationModality
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.catalog.schemas import PluginSummary
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import (
    MANIFEST,
    SetSourceFromBlobArgumentsModel,
    redact_tool_call_arguments,
)
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import _execute_create_blob, _execute_patch_source_options, _execute_set_source_from_blob
from elspeth.web.composer.tools._common import ToolContext as _ToolContext
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY, SOURCE_AUTHORING_KEY
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema
from tests.helpers.session_fences import fenced_operation_context


def _option_shape_summary(*, scalar: int) -> dict[str, object]:
    return {
        "_option_shape": "mapping",
        "entry_count": scalar,
        "value_shape_counts": {
            "mapping": 0,
            "scalar": scalar,
            "sequence": 0,
            "set": 0,
        },
    }


def _empty_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _mock_catalog() -> MagicMock:
    """A minimal catalog whose ``get_schema`` accepts ``text`` (the plugin
    set_source_from_blob defaults to for ``text/plain`` MIME)."""
    catalog = MagicMock(spec=CatalogService)
    catalog.list_sources.return_value = [
        PluginSummary(name=name, description=name, plugin_type="source", config_fields=[]) for name in ("csv", "json", "text")
    ]
    catalog.get_schema.return_value = {"properties": {}}
    return catalog


def ToolContext(*, catalog: CatalogService, **kwargs: Any) -> _ToolContext:
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return _ToolContext(
        catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
        **kwargs,
    )


def _session_engine_with_session() -> tuple[Any, str]:
    engine = create_session_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    initialize_session_schema(engine)
    session_id = str(uuid4())
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            sessions_table.insert().values(
                id=session_id,
                user_id="test-user",
                auth_provider_type="local",
                title="Test Session",
                created_at=now,
                updated_at=now,
            )
        )
    return engine, session_id


def _session_engine_with_user_message(content: str) -> tuple[Any, str, str]:
    engine, session_id = _session_engine_with_session()
    user_message_id = str(uuid4())
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            insert(chat_messages_table).values(
                id=user_message_id,
                session_id=session_id,
                role="user",
                content=content,
                raw_content=None,
                tool_calls=None,
                tool_call_id=None,
                sequence_no=1,
                writer_principal="route_user_message",
                created_at=now,
                composition_state_id=None,
                parent_assistant_id=None,
            )
        )
    return engine, session_id, user_message_id


# ---------------------------------------------------------------------------
# Manifest shape pin
# ---------------------------------------------------------------------------


def test_set_source_from_blob_manifest_entry_is_type_driven() -> None:
    entry = MANIFEST["set_source_from_blob"]
    assert entry.argument_model is SetSourceFromBlobArgumentsModel
    assert entry.policy is None


# ---------------------------------------------------------------------------
# ARG_ERROR routing — bare ValidationError must NOT escape the handler
# ---------------------------------------------------------------------------


class TestPromoteSetSourceFromBlobArgErrorRouting:
    def test_empty_arguments_raise_tool_argument_error(self) -> None:
        """A bare ``{}`` is missing both required fields (blob_id, on_success)."""
        engine, session_id = _session_engine_with_session()
        with pytest.raises(ToolArgumentError) as exc_info:
            _execute_set_source_from_blob(
                {},
                _empty_state(),
                ToolContext(
                    catalog=_mock_catalog(),
                    session_engine=engine,
                    session_id=session_id,
                ),
            )
        assert isinstance(exc_info.value.__cause__, PydanticValidationError)

    def test_non_dict_options_raises_tool_argument_error(self) -> None:
        """Pydantic rejects ``options: str`` before the blob lookup."""
        engine, session_id = _session_engine_with_session()
        with pytest.raises(ToolArgumentError) as exc_info:
            _execute_set_source_from_blob(
                {
                    "blob_id": "anything",
                    "on_success": "out",
                    "options": "column=text",
                },
                _empty_state(),
                ToolContext(
                    catalog=_mock_catalog(),
                    session_engine=engine,
                    session_id=session_id,
                ),
            )
        assert isinstance(exc_info.value.__cause__, PydanticValidationError)

    def test_missing_on_success_raises_tool_argument_error(self) -> None:
        engine, session_id = _session_engine_with_session()
        with pytest.raises(ToolArgumentError) as exc_info:
            _execute_set_source_from_blob(
                {"blob_id": "anything"},
                _empty_state(),
                ToolContext(
                    catalog=_mock_catalog(),
                    session_engine=engine,
                    session_id=session_id,
                ),
            )
        assert isinstance(exc_info.value.__cause__, PydanticValidationError)

    def test_extra_field_raises_tool_argument_error(self) -> None:
        """extra='forbid' rejects fields belonging to neighbouring tools."""
        engine, session_id = _session_engine_with_session()
        with pytest.raises(ToolArgumentError) as exc_info:
            _execute_set_source_from_blob(
                {
                    "blob_id": "anything",
                    "on_success": "out",
                    "content": "hello",  # belongs on create_blob/update_blob
                },
                _empty_state(),
                ToolContext(
                    catalog=_mock_catalog(),
                    session_engine=engine,
                    session_id=session_id,
                ),
            )
        assert isinstance(exc_info.value.__cause__, PydanticValidationError)

    def test_placeholder_blob_id_returns_boundary_failure(self) -> None:
        """Invalid placeholder ids are rejected before blob lookup."""
        engine, session_id = _session_engine_with_session()
        result = _execute_set_source_from_blob(
            {
                "blob_id": "__missing__",
                "on_success": "out",
            },
            _empty_state(),
            ToolContext(
                catalog=_mock_catalog(),
                session_engine=engine,
                session_id=session_id,
            ),
        )

        assert result.success is False
        assert "not a valid UUID" in result.data["error"]
        assert "upload" in result.data["error"]
        assert "list_blobs" in result.data["error"]
        assert "not found" not in result.data["error"].lower()

    def test_valid_arguments_dispatch_normally(self, tmp_path: Path) -> None:
        """Functional smoke: a valid call wires the blob as the source.

        Drives the full create_blob → set_source_from_blob lifecycle so
        the post-promotion handler reaches the source-wiring path
        (versus only the validation gate).
        """
        user_message_content = "Use this exact text:\nhello"
        engine, session_id, user_message_id = _session_engine_with_user_message(user_message_content)
        catalog = _mock_catalog()

        ctx = ToolContext(
            catalog=catalog,
            data_dir=str(tmp_path),
            session_engine=engine,
            session_id=session_id,
            user_message_id=user_message_id,
            user_message_content=user_message_content,
        )
        create_result = _execute_create_blob(
            {"filename": "seed.txt", "mime_type": "text/plain", "content": "hello"},
            _empty_state(),
            ctx,
        )
        assert create_result.success is True
        blob_id = create_result.data["blob_id"]

        bind_result = _execute_set_source_from_blob(
            {
                "blob_id": blob_id,
                "on_success": "out",
                "options": {"column": "text", "schema": {"mode": "observed"}},
            },
            _empty_state(),
            ctx,
        )
        assert bind_result.success is True
        assert bind_result.updated_state.sources["source"].on_success == "out"

    def test_empty_on_validation_failure_canonicalizes_to_discard(self, tmp_path: Path) -> None:
        """elspeth-bcd7051143: this seam used to preserve "" (``is not None``)
        while set_pipeline coerced it — an accepted-then-wedged divergence.
        Every seam now routes through the shared canonicalizer: "" names no
        route (sink names are non-empty), so it persists as 'discard'."""
        user_message_content = "Use this exact text:\nhello"
        engine, session_id, user_message_id = _session_engine_with_user_message(user_message_content)
        catalog = _mock_catalog()

        ctx = ToolContext(
            catalog=catalog,
            data_dir=str(tmp_path),
            session_engine=engine,
            session_id=session_id,
            user_message_id=user_message_id,
            user_message_content=user_message_content,
        )
        create_result = _execute_create_blob(
            {"filename": "seed.txt", "mime_type": "text/plain", "content": "hello"},
            _empty_state(),
            ctx,
        )
        assert create_result.success is True

        bind_result = _execute_set_source_from_blob(
            {
                "blob_id": create_result.data["blob_id"],
                "on_success": "out",
                "options": {"column": "text", "schema": {"mode": "observed"}},
                "on_validation_failure": "",
            },
            _empty_state(),
            ctx,
        )
        assert bind_result.success is True
        assert bind_result.updated_state.sources["source"].on_validation_failure == "discard"

    def test_omitted_options_validates_at_model_layer(self) -> None:
        """``options`` is optional at the model layer (default ``{}``).

        Pin against a future refactor that changes ``options`` to
        ``Optional[dict] = None`` (which would produce ``"null"`` in
        the redacted dict — see model docstring).  The downstream
        plugin-side validation in ``_resolve_source_blob`` may still
        reject an empty ``options`` if the inferred plugin requires
        specific keys; that is the source plugin's contract, not the
        argument-model's.

        We exercise the model directly here so the test is independent
        of which source plugin is inferred from a given MIME type — that
        inference belongs to the handler's runtime path, not the
        argument-validation gate this test pins.
        """
        validated = SetSourceFromBlobArgumentsModel.model_validate({"blob_id": "anything", "on_success": "out"})
        assert validated.options == {}, (
            "Omitting options must default to {} (not None). A None default "
            "would produce 'null' in the redacted dict via the summarizer, "
            "diverging from the handler's runtime semantics where an absent "
            "options slot is treated as no caller-supplied options."
        )
        # plugin and on_validation_failure preserve None-vs-specified
        # semantics (so the handler can apply the right fallback).
        assert validated.plugin is None
        assert validated.on_validation_failure is None

    def test_llm_authored_blob_binding_stamps_source_authoring_without_unlocking_path(self, tmp_path: Path) -> None:
        """Blob-backed source provenance is stamped while path/blob_ref stay locked."""
        user_message_content = "Create generated text content for the source."
        engine, session_id, user_message_id = _session_engine_with_user_message(user_message_content)
        catalog = _mock_catalog()
        ctx = ToolContext(
            catalog=catalog,
            data_dir=str(tmp_path),
            session_engine=engine,
            session_id=session_id,
            user_message_id=user_message_id,
            user_message_content=user_message_content,
            composer_model_identifier="openai/gpt-5-mini",
            composer_model_version="gpt-5-mini-2026-05-01",
            composer_provider="openai",
            composer_skill_hash="a" * 64,
            tool_arguments_hash="b" * 64,
        )
        create_result = _execute_create_blob(
            {"filename": "generated.txt", "mime_type": "text/plain", "content": "generated row text"},
            _empty_state(),
            ctx,
        )
        assert create_result.success is True

        bind_result = _execute_set_source_from_blob(
            {
                "blob_id": create_result.data["blob_id"],
                "on_success": "out",
                "options": {"column": "text", "schema": {"mode": "observed"}},
            },
            _empty_state(),
            ctx,
        )

        assert bind_result.success is True, bind_result.data
        assert "source" in bind_result.updated_state.sources
        options = bind_result.updated_state.sources["source"].options
        assert options[SOURCE_AUTHORING_KEY] == {
            "modality": CreationModality.LLM_GENERATED.value,
            "content_hash": create_result.data["content_hash"],
            "review_event_id": None,
            "resolved_kind": None,
        }
        requirement = options[INTERPRETATION_REQUIREMENTS_KEY][0]
        assert requirement == {
            "id": "source_review:inline_source_data",
            "kind": "invented_source",
            "user_term": "inline_source_data",
            "status": "pending",
            "draft": "generated row text",
            "event_id": None,
            "accepted_value": None,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": None,
        }

        forged_authoring_patch = _execute_patch_source_options(
            {
                "patch": {
                    SOURCE_AUTHORING_KEY: {
                        "modality": CreationModality.VERBATIM.value,
                        "content_hash": "0" * 64,
                        "review_event_id": "forged-review",
                        "resolved_kind": "forged-kind",
                    }
                }
            },
            bind_result.updated_state,
            ctx,
        )
        assert forged_authoring_patch.success is False
        assert SOURCE_AUTHORING_KEY in forged_authoring_patch.data["error"]

        patch_result = _execute_patch_source_options(
            {"patch": {"path": str(tmp_path / "other.txt")}},
            bind_result.updated_state,
            ctx,
        )
        assert patch_result.success is False
        assert "Cannot patch" in patch_result.data["error"]


# ---------------------------------------------------------------------------
# Redaction at the persistence boundary
# ---------------------------------------------------------------------------


_CANARY = "CANARY-SET-SOURCE-FROM-BLOB-PATH-DO-NOT-LEAK"


def test_redaction_substitutes_options_via_summarizer() -> None:
    """``options`` is replaced by the canonical-JSON shape summary.

    Uniformity-with-set_source contract: both source-binding tools share
    the same summarizer.  The summary must preserve shape without echoing
    plugin option values such as paths, blob refs, or credentials.
    """
    tel = NoopRedactionTelemetry()
    args = {
        "blob_id": "some-blob-id",
        "on_success": "out",
        "options": {"path": _CANARY, "blob_ref": "abc123"},
    }
    redacted = redact_tool_call_arguments("set_source_from_blob", args, telemetry=tel)
    # Structural keys preserved.
    assert redacted["blob_id"] == "some-blob-id"
    assert redacted["on_success"] == "out"
    # options is now the summarizer's str output.
    assert isinstance(redacted["options"], str)
    assert json.loads(redacted["options"]) == _option_shape_summary(scalar=2)
    # The canary value MUST NOT appear in the redacted dict OR its JSON form.
    serialized = json.dumps(redacted, sort_keys=True)
    assert _CANARY not in serialized
    # Telemetry recorded the manifest dispatch with the type-driven shape.
    assert tel.manifest_dispatch_calls == [{"tool_name": "set_source_from_blob", "shape": "type_driven"}]


def test_redaction_hides_paths_without_blob_ref() -> None:
    """Path values are redacted even when no blob_ref marker is present."""
    tel = NoopRedactionTelemetry()
    args = {
        "blob_id": "id",
        "on_success": "out",
        "options": {"path": "/tmp/data.csv"},
    }
    redacted = redact_tool_call_arguments("set_source_from_blob", args, telemetry=tel)
    assert isinstance(redacted["options"], str)
    assert json.loads(redacted["options"]) == _option_shape_summary(scalar=1)
    assert "/tmp/data.csv" not in redacted["options"]


# ---------------------------------------------------------------------------
# TSV-delimiter parity (bug elspeth-da09ed23d4)
# ---------------------------------------------------------------------------


class TestSetSourceFromBlobTsvDelimiter:
    """A ``.tsv`` blob (uploaded as ``text/csv``) must bind a csv source whose
    ``delimiter`` is a tab, matching what ``inspect_blob_content`` reports.

    Without this, ``CSVSourceConfig.delimiter`` defaults to comma and the
    tab-separated rows parse as a single column at runtime — the inspect-vs-bind
    parity gap the ticket names.
    """

    def _bind_csv_blob(
        self,
        *,
        filename: str,
        content: str,
        tmp_path: Path,
        options: dict[str, Any] | None = None,
    ) -> Any:
        # Embed the blob content verbatim in the user message so the blob is
        # classified VERBATIM (operator-supplied), not LLM-authored — that keeps
        # the harness free of full composer provenance just to exercise the
        # delimiter-derivation path.
        user_message_content = f"Bind this tabular blob as the source:\n{content}"
        engine, session_id, user_message_id = _session_engine_with_user_message(user_message_content)
        catalog = _mock_catalog()
        ctx = ToolContext(
            catalog=catalog,
            data_dir=str(tmp_path),
            session_engine=engine,
            session_id=session_id,
            user_message_id=user_message_id,
            user_message_content=user_message_content,
        )
        create_result = _execute_create_blob(
            {"filename": filename, "mime_type": "text/csv", "content": content},
            _empty_state(),
            ctx,
        )
        assert create_result.success is True, create_result.data
        bind_result = _execute_set_source_from_blob(
            {
                "blob_id": create_result.data["blob_id"],
                "on_success": "out",
                "options": options if options is not None else {"schema": {"mode": "observed"}},
            },
            _empty_state(),
            ctx,
        )
        return bind_result

    def test_tsv_blob_binds_csv_source_with_tab_delimiter(self, tmp_path: Path) -> None:
        bind_result = self._bind_csv_blob(
            filename="data.tsv",
            content="a\tb\tc\n1\t2\t3\n",
            tmp_path=tmp_path,
        )
        assert bind_result.success is True, bind_result.data
        source = bind_result.updated_state.sources["source"]
        assert source.plugin == "csv"
        assert source.options.get("delimiter") == "\t"

    def test_caller_supplied_delimiter_is_not_overridden(self, tmp_path: Path) -> None:
        bind_result = self._bind_csv_blob(
            filename="data.tsv",
            content="a;b;c\n1;2;3\n",
            tmp_path=tmp_path,
            options={"delimiter": ";", "schema": {"mode": "observed"}},
        )
        assert bind_result.success is True, bind_result.data
        source = bind_result.updated_state.sources["source"]
        assert source.options.get("delimiter") == ";"

    def test_csv_blob_does_not_inject_delimiter(self, tmp_path: Path) -> None:
        bind_result = self._bind_csv_blob(
            filename="data.csv",
            content="a,b,c\n1,2,3\n",
            tmp_path=tmp_path,
        )
        assert bind_result.success is True, bind_result.data
        source = bind_result.updated_state.sources["source"]
        assert source.plugin == "csv"
        assert source.options.get("delimiter") is None


# ---------------------------------------------------------------------------
# Content-derived guaranteed_fields (elspeth-da68332faf)
# ---------------------------------------------------------------------------


class TestSetSourceFromBlobDerivedGuarantees:
    """An LLM-AUTHORED bound blob IS the run's data, so its bind auto-declares
    ``schema.guaranteed_fields`` — verified per row against the actual content,
    all-or-nothing, never partial, and never over an author-written
    declaration. An UPLOADED/verbatim blob's header is a SAMPLE (John's ruling
    2026-08-27) and never auto-declares: it feeds the ask-the-user flow
    (elspeth-da68332faf work item 2)."""

    def _bind_blob(
        self,
        *,
        filename: str,
        content: str,
        mime_type: str,
        tmp_path: Path,
        options: dict[str, Any] | None = None,
        authored: bool = True,
    ) -> Any:
        # ``authored=True`` keeps the blob content OUT of the user message so
        # provenance classifies it LLM_GENERATED and the bind stamps
        # SOURCE_AUTHORING_KEY — the evidence class auto-declare is scoped to.
        # ``authored=False`` embeds the content verbatim (user-supplied).
        if authored:
            user_message_content = "Generate the source data yourself."
        else:
            user_message_content = f"Bind this blob as the source:\n{content}"
        engine, session_id, user_message_id = _session_engine_with_user_message(user_message_content)
        catalog = _mock_catalog()
        ctx = ToolContext(
            catalog=catalog,
            data_dir=str(tmp_path),
            session_engine=engine,
            session_id=session_id,
            user_message_id=user_message_id,
            user_message_content=user_message_content,
            composer_model_identifier="openai/gpt-5-mini",
            composer_model_version="gpt-5-mini-2026-05-01",
            composer_provider="openai",
            composer_skill_hash="a" * 64,
            tool_arguments_hash="b" * 64,
        )
        create_result = _execute_create_blob(
            {"filename": filename, "mime_type": mime_type, "content": content},
            _empty_state(),
            ctx,
        )
        assert create_result.success is True, create_result.data
        bind_result = _execute_set_source_from_blob(
            {
                "blob_id": create_result.data["blob_id"],
                "on_success": "out",
                "options": options if options is not None else {"schema": {"mode": "observed"}},
            },
            _empty_state(),
            ctx,
        )
        return bind_result

    def _schema(self, bind_result: Any) -> dict[str, Any]:
        assert bind_result.success is True, bind_result.data
        return dict(bind_result.updated_state.sources["source"].options["schema"])

    def test_authored_csv_bind_stamps_complete_header_guarantee(self, tmp_path: Path) -> None:
        schema = self._schema(
            self._bind_blob(
                filename="data.csv",
                content="id,Score Value,colour\n1,2,red\n",
                mime_type="text/csv",
                tmp_path=tmp_path,
            )
        )
        # Headers map through declarable_field_name to canonical row keys.
        assert schema["mode"] == "observed"
        assert list(schema["guaranteed_fields"]) == ["id", "score_value", "colour"]

    def test_uploaded_csv_bind_never_auto_declares(self, tmp_path: Path) -> None:
        """The negative the ruling requires: a user-verbatim CSV blob binds
        successfully and gains NO guaranteed_fields — its header is a sample."""
        bind_result = self._bind_blob(
            filename="data.csv",
            content="id,colour\n1,red\n",
            mime_type="text/csv",
            tmp_path=tmp_path,
            authored=False,
        )
        schema = self._schema(bind_result)
        assert schema == {"mode": "observed"}
        # And the bind really was the verbatim class, not a mis-classified one.
        assert SOURCE_AUTHORING_KEY not in bind_result.updated_state.sources["source"].options

    def test_authored_csv_bind_without_schema_block_creates_observed_guarantee(self, tmp_path: Path) -> None:
        schema = self._schema(
            self._bind_blob(
                filename="data.csv",
                content="a,b\n1,2\n",
                mime_type="text/csv",
                tmp_path=tmp_path,
                options={},
            )
        )
        assert schema["mode"] == "observed"
        assert list(schema["guaranteed_fields"]) == ["a", "b"]

    def test_authored_tsv_bind_reads_header_with_derived_tab_delimiter(self, tmp_path: Path) -> None:
        schema = self._schema(
            self._bind_blob(
                filename="data.tsv",
                content="alpha\tbeta\n1\t2\n",
                mime_type="text/csv",
                tmp_path=tmp_path,
            )
        )
        assert list(schema["guaranteed_fields"]) == ["alpha", "beta"]

    def test_ragged_row_stamps_full_header_because_the_source_quarantines_it(self, tmp_path: Path) -> None:
        """Per-row presence derives from the csv source's own materialization:
        a record with the wrong cell count is QUARANTINED by column-count
        validation ("expected N fields, got M") and never emits as a valid
        row, so it cannot shrink the guarantee — every VALID row is
        ``dict(zip(headers, values))`` and carries every header key, which is
        exactly the key-presence predicate ``verify_source_guaranteed_fields``
        (ADR-016) enforces. The premise is pinned against the real plugin by
        ``test_ragged_row_premise_quarantine_not_padding`` below."""
        schema = self._schema(
            self._bind_blob(
                filename="data.csv",
                content="a,b\n1\n2,3\n",
                mime_type="text/csv",
                tmp_path=tmp_path,
            )
        )
        assert list(schema["guaranteed_fields"]) == ["a", "b"]

    def test_ragged_row_premise_quarantine_not_padding(self, tmp_path: Path) -> None:
        """Predicate-parity pin tying the stamp to the runtime authority: feed
        the REAL csv source the ragged content and assert the ragged record is
        quarantined (never padded into a valid row) and every valid row
        carries every header key. If the csv source ever starts padding short
        rows, this fails first — and the bind-time derivation must become an
        intersection over emitted-row key sets."""
        from elspeth.plugins.sources.csv_source import CSVSource

        csv_path = tmp_path / "ragged.csv"
        csv_path.write_text("a,b\n1\n2,3\n", encoding="utf-8")
        source = CSVSource(
            {
                "path": str(csv_path),
                "schema": {"mode": "observed"},
                "on_validation_failure": "quarantine_sink",
            }
        )

        class _Ctx:
            def record_validation_error(self, **kwargs: Any) -> None:
                del kwargs

        rows = list(source.load(_Ctx()))
        valid = [row for row in rows if not row.is_quarantined]
        quarantined = [row for row in rows if row.is_quarantined]
        assert len(valid) == 1, rows
        assert set(valid[0].row.keys()) >= {"a", "b"}
        assert len(quarantined) == 1
        assert quarantined[0].quarantine_error is not None
        assert "expected 2 fields, got 1" in quarantined[0].quarantine_error

    def test_runtime_predicate_is_key_presence_not_value_nonemptiness(self) -> None:
        """The other half of predicate parity: the stamp treats an empty VALUE
        as data because ``verify_source_guaranteed_fields`` (ADR-016) checks
        ``row_data.keys()`` — KEY presence. Exercise the runtime authority
        directly on that exact discriminating pair: if its definition ever
        tightens to value non-emptiness, the empty-value arm here fails before
        any stamped pipeline over-claims."""
        from elspeth.contracts.errors import SourceGuaranteedFieldsViolation
        from elspeth.engine.executors.source_guaranteed_fields import (
            _build_contract,
            verify_source_guaranteed_fields,
        )

        common: dict[str, Any] = {
            "declared_guaranteed_fields": frozenset({"a", "b"}),
            "row_contract": _build_contract(("a", "b")),
            "plugin_name": "csv",
            "node_id": "source",
            "run_id": "run",
            "row_id": "row-1",
            "token_id": "token-1",
        }
        # Empty-string values: every key present — the guarantee holds.
        verify_source_guaranteed_fields(row_data={"a": "", "b": ""}, **common)
        # Absent key: the guarantee is violated regardless of sibling values.
        with pytest.raises(SourceGuaranteedFieldsViolation) as excinfo:
            verify_source_guaranteed_fields(row_data={"a": ""}, **common)
        assert list(excinfo.value.payload["missing"]) == ["b"]

    def test_all_empty_values_row_is_data_not_absence(self, tmp_path: Path) -> None:
        """A row spelled ``,,`` carries every field as an EMPTY VALUE — key
        presence is what the runtime contract checks, so it must not exclude
        any field from the stamp."""
        schema = self._schema(
            self._bind_blob(
                filename="data.csv",
                content="a,b,c\n,,\n",
                mime_type="text/csv",
                tmp_path=tmp_path,
            )
        )
        assert list(schema["guaranteed_fields"]) == ["a", "b", "c"]

    def test_header_only_content_stamps_nothing(self, tmp_path: Path) -> None:
        """Zero valid data rows is zero per-row evidence — abstain entirely
        (never stamp an empty list: () participates and guarantees nothing)."""
        schema = self._schema(
            self._bind_blob(
                filename="data.csv",
                content="a,b\n",
                mime_type="text/csv",
                tmp_path=tmp_path,
            )
        )
        assert "guaranteed_fields" not in schema

    def test_all_ragged_content_stamps_nothing(self, tmp_path: Path) -> None:
        """Every data record would quarantine, so no valid row ever emits —
        no per-row evidence, abstain."""
        schema = self._schema(
            self._bind_blob(
                filename="data.csv",
                content="a,b\n1\n",
                mime_type="text/csv",
                tmp_path=tmp_path,
            )
        )
        assert "guaranteed_fields" not in schema

    def test_undeclarable_header_stamps_nothing_all_or_nothing(self, tmp_path: Path) -> None:
        """One header with no declarable form abstains ENTIRELY — a partial
        guaranteed_fields is a complete-claim violation (SchemaConfig)."""
        schema = self._schema(
            self._bind_blob(
                filename="data.csv",
                content="id,!!!\n1,2\n",
                mime_type="text/csv",
                tmp_path=tmp_path,
            )
        )
        assert "guaranteed_fields" not in schema

    def test_colliding_canonical_headers_stamp_nothing(self, tmp_path: Path) -> None:
        """Two headers collapsing onto one canonical row key are ambiguous
        evidence, so the bind abstains rather than guessing."""
        schema = self._schema(
            self._bind_blob(
                filename="data.csv",
                content="A B,a_b\n1,2\n",
                mime_type="text/csv",
                tmp_path=tmp_path,
            )
        )
        assert "guaranteed_fields" not in schema

    def test_jsonl_bind_does_not_auto_declare(self, tmp_path: Path) -> None:
        """A sampled key-union is not per-row evidence — JSON/JSONL abstain."""
        bind_result = self._bind_blob(
            filename="data.jsonl",
            content='{"a": 1}\n{"a": 2, "b": 3}\n',
            mime_type="application/x-jsonlines",
            tmp_path=tmp_path,
            options={"schema": {"mode": "observed"}},
        )
        schema = self._schema(bind_result)
        assert "guaranteed_fields" not in schema

    def test_columns_configured_csv_still_binds_green_and_unstamped(self, tmp_path: Path) -> None:
        """Bind-regression pin: ``columns`` replaces the observed header as the
        row-key authority, so a header-derived guarantee could be unreachable
        (check_declared_fields_reachable) — the bind must stay green and stamp
        nothing."""
        bind_result = self._bind_blob(
            filename="data.csv",
            content="one,two\n1,2\n",
            mime_type="text/csv",
            tmp_path=tmp_path,
            options={"columns": ["first", "second"], "schema": {"mode": "observed"}},
        )
        schema = self._schema(bind_result)
        assert "guaranteed_fields" not in schema

    def test_field_mapping_configured_csv_still_binds_green_and_unstamped(self, tmp_path: Path) -> None:
        bind_result = self._bind_blob(
            filename="data.csv",
            content="one,two\n1,2\n",
            mime_type="text/csv",
            tmp_path=tmp_path,
            options={"field_mapping": {"one": "renamed_one"}, "schema": {"mode": "observed"}},
        )
        schema = self._schema(bind_result)
        assert "guaranteed_fields" not in schema

    def test_skip_rows_configured_csv_stamps_nothing(self, tmp_path: Path) -> None:
        """With skip_rows the runtime header is a different record than the
        first one read here — abstain rather than guarantee the wrong row."""
        bind_result = self._bind_blob(
            filename="data.csv",
            content="preamble line\na,b\n1,2\n",
            mime_type="text/csv",
            tmp_path=tmp_path,
            options={"skip_rows": 1, "schema": {"mode": "observed"}},
        )
        schema = self._schema(bind_result)
        assert "guaranteed_fields" not in schema

    def test_author_written_guarantee_is_never_widened(self, tmp_path: Path) -> None:
        bind_result = self._bind_blob(
            filename="data.csv",
            content="a,b\n1,2\n",
            mime_type="text/csv",
            tmp_path=tmp_path,
            options={"schema": {"mode": "observed", "guaranteed_fields": ["a"]}},
        )
        schema = self._schema(bind_result)
        assert list(schema["guaranteed_fields"]) == ["a"]

    def test_non_utf8_csv_blob_still_binds_and_abstains(self, tmp_path: Path) -> None:
        """An uploaded latin-1 CSV stays bindable and unstamped: it is
        user-verbatim (no source_authoring), so auto-declare never engages."""
        import asyncio
        from uuid import UUID

        from elspeth.web.blobs.service import BlobServiceImpl

        engine, session_id = _session_engine_with_session()
        blob_service = BlobServiceImpl(engine, tmp_path)
        with fenced_operation_context(engine, session_id, operation_kind=SessionOperationKind.CREATE) as create_context:
            record = asyncio.run(
                blob_service.create_blob(
                    session_id=UUID(session_id),
                    filename="latin.csv",
                    content="colonne,prix\ncaf\xe9,3\n".encode("latin-1"),
                    mime_type="text/csv",  # type: ignore[arg-type]
                    created_by="user",
                    source_description="uploaded",
                    session_operation_context=create_context,
                )
            )
        ctx = ToolContext(
            catalog=_mock_catalog(),
            data_dir=str(tmp_path),
            session_engine=engine,
            session_id=session_id,
        )
        bind_result = _execute_set_source_from_blob(
            {
                "blob_id": str(record.id),
                "on_success": "out",
                "options": {"schema": {"mode": "observed"}, "encoding": "latin-1"},
            },
            _empty_state(),
            ctx,
        )
        schema = self._schema(bind_result)
        assert "guaranteed_fields" not in schema

    def test_non_utf8_encoding_option_abstains_even_for_authored_utf8_bytes(self, tmp_path: Path) -> None:
        """The runtime will read the file with the configured encoding; when
        that is not UTF-8 the bind-time evidence may not match, so abstain."""
        bind_result = self._bind_blob(
            filename="data.csv",
            content="a,b\n1,2\n",
            mime_type="text/csv",
            tmp_path=tmp_path,
            options={"schema": {"mode": "observed"}, "encoding": "latin-1"},
        )
        schema = self._schema(bind_result)
        assert "guaranteed_fields" not in schema


class TestEchoedServerOwnedMetadata:
    """Echo-tolerant reserved-key gates on the singular source tools
    (elspeth-c67fbbbd83, option ii): an exact echo of stored server-owned
    metadata is reduced/dropped with an advisory note; any non-matching value
    keeps the elspeth-4496f61e30 rejection."""

    def _bound_source(self, tmp_path: Path) -> tuple[_ToolContext, CompositionState, dict[str, Any]]:
        user_message_content = "Create generated CSV content for the source."
        engine, session_id, user_message_id = _session_engine_with_user_message(user_message_content)
        ctx = ToolContext(
            catalog=_mock_catalog(),
            data_dir=str(tmp_path),
            session_engine=engine,
            session_id=session_id,
            user_message_id=user_message_id,
            user_message_content=user_message_content,
            composer_model_identifier="openai/gpt-5-mini",
            composer_model_version="gpt-5-mini-2026-05-01",
            composer_provider="openai",
            composer_skill_hash="a" * 64,
            tool_arguments_hash="b" * 64,
        )
        create = _execute_create_blob(
            {"filename": "generated.csv", "mime_type": "text/csv", "content": "name,score\nada,42\n"},
            _empty_state(),
            ctx,
        )
        assert create.success is True
        bind = _execute_set_source_from_blob(
            {
                "blob_id": create.data["blob_id"],
                "on_success": "rows",
                "options": {"schema": {"mode": "observed"}},
            },
            _empty_state(),
            ctx,
        )
        assert bind.success is True, bind.data
        options = dict(deep_thaw(bind.updated_state.sources["source"].options))
        return ctx, bind.updated_state, options

    @staticmethod
    def _resolved_state(state: CompositionState, options: dict[str, Any]) -> tuple[CompositionState, dict[str, Any]]:
        """Project the bound source's pending review into its resolved form,
        the state the review resolver persists (elspeth session 2e0c8ea3's
        v3: authoring block review-bound, requirement resolved)."""
        authoring = {
            **options[SOURCE_AUTHORING_KEY],
            "review_event_id": "event-1",
            "resolved_kind": "invented_source",
        }
        resolved_row = {
            **options[INTERPRETATION_REQUIREMENTS_KEY][0],
            "status": "resolved",
            "event_id": "event-1",
            "accepted_value": "approved",
            "accepted_artifact_hash": authoring["content_hash"],
        }
        resolved_options = {
            **options,
            SOURCE_AUTHORING_KEY: authoring,
            INTERPRETATION_REQUIREMENTS_KEY: [resolved_row],
        }
        source = state.sources["source"]
        return state.with_named_source("source", replace(source, options=resolved_options)), resolved_options

    def test_patch_echoing_stored_pending_rows_is_accepted_and_preserved(self, tmp_path: Path) -> None:
        ctx, state, options = self._bound_source(tmp_path)

        result = _execute_patch_source_options(
            {"patch": {INTERPRETATION_REQUIREMENTS_KEY: options[INTERPRETATION_REQUIREMENTS_KEY]}},
            state,
            ctx,
        )

        assert result.success is True, result.data
        assert "interpretation_requirements" in result.data["server_owned_metadata_note"]
        requirements = deep_thaw(result.updated_state.sources["source"].options[INTERPRETATION_REQUIREMENTS_KEY])
        assert requirements == options[INTERPRETATION_REQUIREMENTS_KEY]

    def test_patch_echoing_reduced_resolved_row_keeps_the_review_resolved(self, tmp_path: Path) -> None:
        """The planner-context projection of a resolved row round-trips: the
        echo reduces to a shell and reconciliation restores the resolved
        server row — no downgrade to pending."""
        ctx, state, options = self._bound_source(tmp_path)
        state, resolved_options = self._resolved_state(state, options)
        stored_row = resolved_options[INTERPRETATION_REQUIREMENTS_KEY][0]
        context_projection = {field: stored_row[field] for field in ("id", "kind", "user_term", "draft", "status")}

        result = _execute_patch_source_options(
            {"patch": {INTERPRETATION_REQUIREMENTS_KEY: [context_projection]}},
            state,
            ctx,
        )

        assert result.success is True, result.data
        carried = deep_thaw(result.updated_state.sources["source"].options[INTERPRETATION_REQUIREMENTS_KEY])[0]
        assert carried == stored_row
        authoring = deep_thaw(result.updated_state.sources["source"].options[SOURCE_AUTHORING_KEY])
        assert authoring == resolved_options[SOURCE_AUTHORING_KEY]

    def test_patch_with_tampered_resolved_row_still_rejects(self, tmp_path: Path) -> None:
        ctx, state, options = self._bound_source(tmp_path)
        state, resolved_options = self._resolved_state(state, options)
        stored_row = resolved_options[INTERPRETATION_REQUIREMENTS_KEY][0]
        tampered = {field: stored_row[field] for field in ("id", "kind", "user_term", "draft", "status")}
        tampered["draft"] = "tampered draft"

        result = _execute_patch_source_options(
            {"patch": {INTERPRETATION_REQUIREMENTS_KEY: [tampered]}},
            state,
            ctx,
        )

        assert result.success is False
        assert "resolved" in result.data["error"]

    def test_rebind_echoing_stored_source_authoring_is_accepted_with_note(self, tmp_path: Path) -> None:
        ctx, state, options = self._bound_source(tmp_path)

        result = _execute_set_source_from_blob(
            {
                "blob_id": options["blob_ref"],
                "on_success": "rows",
                "options": {
                    "schema": options["schema"],
                    SOURCE_AUTHORING_KEY: options[SOURCE_AUTHORING_KEY],
                },
            },
            state,
            ctx,
        )

        assert result.success is True, result.data
        assert "source_authoring" in result.data["server_owned_metadata_note"]
        assert deep_thaw(result.updated_state.sources["source"].options[SOURCE_AUTHORING_KEY]) == options[SOURCE_AUTHORING_KEY]

    def test_rebind_with_tampered_source_authoring_still_rejects(self, tmp_path: Path) -> None:
        ctx, state, options = self._bound_source(tmp_path)
        tampered = {**options[SOURCE_AUTHORING_KEY], "review_event_id": "forged-event"}

        result = _execute_set_source_from_blob(
            {
                "blob_id": options["blob_ref"],
                "on_success": "rows",
                "options": {"schema": options["schema"], SOURCE_AUTHORING_KEY: tampered},
            },
            state,
            ctx,
        )

        assert result.success is False
        assert SOURCE_AUTHORING_KEY in result.data["error"]
