"""Regression tests for the P3 Dataverse / Chroma sweep findings (20261111-plugins-review).

Each test class names the sweep finding it pins.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError

_ENV_URL = "https://myorg.crm.dynamics.com"
_AUTH = {
    "method": "service_principal",
    "tenant_id": "tenant-123",
    "client_id": "client-456",
    "client_secret": "secret-789",  # secret-scan: allow-this-line
}


def _source_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "environment_url": _ENV_URL,
        "auth": _AUTH,
        "entity": "contact",
        "schema": {"mode": "observed"},
        "on_validation_failure": "quarantine",
    }
    config.update(overrides)
    return config


class TestFinding48SelectAndTopValidation:
    """#48: select entries must be Dataverse identifiers; top must be >= 1."""

    @pytest.mark.parametrize(
        "select_field",
        ["fullname#x", "fullname&$filter=statecode eq 1", "full name", "fullname,emailaddress1", "FullName", "a/b"],
    )
    def test_non_identifier_select_entry_rejected_at_config_time(self, select_field: str) -> None:
        from elspeth.plugins.sources.dataverse import DataverseSourceConfig

        with pytest.raises(PluginConfigError, match=r"select\[0\]"):
            DataverseSourceConfig.from_dict(_source_config(select=[select_field]))

    @pytest.mark.parametrize("select_field", ["fullname", "_parentcustomerid_value", " emailaddress1 "])
    def test_identifier_select_entries_still_accepted(self, select_field: str) -> None:
        from elspeth.plugins.sources.dataverse import DataverseSourceConfig

        cfg = DataverseSourceConfig.from_dict(_source_config(select=[select_field]))
        assert cfg.select == [select_field.strip()]

    @pytest.mark.parametrize("top", [0, -1])
    def test_non_positive_top_rejected_at_config_time(self, top: int) -> None:
        from elspeth.plugins.sources.dataverse import DataverseSourceConfig

        with pytest.raises(PluginConfigError, match="top"):
            DataverseSourceConfig.from_dict(_source_config(top=top))


def _fetchxml_source_config(fetch_xml: str) -> dict[str, Any]:
    config = _source_config(fetch_xml=fetch_xml)
    del config["entity"]
    return config


class TestFinding49FetchXmlEntityRequiredAtConfigTime:
    """#49: FetchXML must name exactly one <entity> under <fetch>, checked at config time."""

    @pytest.mark.parametrize(
        "fetch_xml",
        [
            "<fetch><all-attributes/></fetch>",
            "<fetch><entity><attribute name='fullname'/></entity></fetch>",
            "<fetch><entity name='contact'/><entity name='account'/></fetch>",
        ],
        ids=["no-entity", "entity-without-name", "two-entities"],
    )
    def test_fetchxml_without_exactly_one_named_entity_rejected(self, fetch_xml: str) -> None:
        from elspeth.plugins.sources.dataverse import DataverseSourceConfig

        with pytest.raises(PluginConfigError, match="entity"):
            DataverseSourceConfig.from_dict(_fetchxml_source_config(fetch_xml))

    def test_fetchxml_with_one_named_entity_accepted(self) -> None:
        from elspeth.plugins.sources.dataverse import DataverseSourceConfig

        xml = "<fetch><entity name='contact'><attribute name='fullname'/></entity></fetch>"
        assert DataverseSourceConfig.from_dict(_fetchxml_source_config(xml)).fetch_xml == xml


class _RecordedCall:
    def __init__(self, call_id: str) -> None:
        self.call_id = call_id


class _ChromaExecutionRecorder:
    """Records the provider's audit writes; mirrors the retrieval test fake."""

    def __init__(self) -> None:
        self.recorded_calls: list[dict[str, Any]] = []
        self.operation_calls: list[dict[str, Any]] = []
        self.operation_indices_allocated = 0
        self.state_indices_allocated = 0

    def allocate_call_index(self, state_id: str, *, member_token: Any, work_item: Any) -> int:
        self.state_indices_allocated += 1
        return self.state_indices_allocated - 1

    def record_call(self, **kwargs: Any) -> _RecordedCall:
        self.recorded_calls.append(kwargs)
        return _RecordedCall(f"call-{len(self.recorded_calls)}")

    def allocate_operation_call_index(self, operation_id: str, *, coordination_token: Any) -> int:
        self.operation_indices_allocated += 1
        return self.operation_indices_allocated - 1

    def record_operation_call(self, **kwargs: Any) -> _RecordedCall:
        self.operation_calls.append(kwargs)
        return _RecordedCall(f"operation-call-{len(self.operation_calls)}")


class _UnreachableCountCollection:
    def count(self) -> int:
        raise ConnectionError("connection refused")


class TestFinding75ChromaPreflightRecordsFailedCount:
    """#75: an attempted readiness count that fails leaves an ERROR Call row (and is verified in VERIFY)."""

    @staticmethod
    def _provider(collection: str, *, session: Any = None, execution: _ChromaExecutionRecorder) -> Any:
        from elspeth.plugins.infrastructure.clients.retrieval.chroma import ChromaSearchProvider, ChromaSearchProviderConfig

        return ChromaSearchProvider(
            config=ChromaSearchProviderConfig(collection=collection, mode="ephemeral"),
            execution=execution,
            run_id="p3-run",
            call_mode_session=session,
        )

    @staticmethod
    def _existing_collection() -> str:
        import uuid

        chromadb = pytest.importorskip("chromadb")
        name = f"p3-ready-{uuid.uuid4().hex[:12]}"
        chromadb.Client().get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})
        return name

    def test_live_unreachable_count_records_error_operation_call(self) -> None:
        from elspeth.contracts.enums import CallStatus
        from tests.fixtures.mock_audit import mock_audit_authority

        execution = _ChromaExecutionRecorder()
        provider = self._provider(self._existing_collection(), execution=execution)
        provider._collection = _UnreachableCountCollection()

        readiness = provider.runtime_preflight(operation_id="op-1", coordination_token=mock_audit_authority()["coordination_token"])

        assert readiness.reachable is False
        assert execution.operation_indices_allocated == 1
        assert len(execution.operation_calls) == 1
        call = execution.operation_calls[0]
        assert call["status"] is CallStatus.ERROR
        assert call["call_index"] == 0
        error = call["error"].to_dict()
        assert error["error_type"] == "ConnectionError"
        assert "connection refused" in error["message"]

    def test_verify_unreachable_count_reaches_verify_call(self) -> None:
        from unittest.mock import MagicMock

        from elspeth.contracts.call_mode import CallModeSession
        from elspeth.contracts.enums import CallStatus, RunMode
        from tests.fixtures.mock_audit import mock_audit_authority

        session = MagicMock(spec=CallModeSession)
        session.mode = RunMode.VERIFY
        execution = _ChromaExecutionRecorder()
        provider = self._provider(self._existing_collection(), session=session, execution=execution)
        provider._initialize_client()
        provider._collection = _UnreachableCountCollection()

        readiness = provider.runtime_preflight(operation_id="op-1", coordination_token=mock_audit_authority()["coordination_token"])

        assert readiness.reachable is False
        assert [c["status"] for c in execution.operation_calls] == [CallStatus.ERROR]
        session.verify_call.assert_called_once()
        assert session.verify_call.call_args.kwargs["live_status"] is CallStatus.ERROR
        assert session.verify_call.call_args.kwargs["current_call_id"] == "operation-call-1"

    def test_verify_missing_collection_during_preflight_records_error_before_raising(self) -> None:
        from unittest.mock import MagicMock

        from elspeth.contracts.call_mode import CallModeSession
        from elspeth.contracts.enums import CallStatus, RunMode
        from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalError
        from tests.fixtures.mock_audit import mock_audit_authority

        session = MagicMock(spec=CallModeSession)
        session.mode = RunMode.VERIFY
        execution = _ChromaExecutionRecorder()
        provider = self._provider("p3-missing-collection-preflight", session=session, execution=execution)

        with pytest.raises(RetrievalError, match="does not exist"):
            provider.runtime_preflight(operation_id="op-1", coordination_token=mock_audit_authority()["coordination_token"])

        assert [c["status"] for c in execution.operation_calls] == [CallStatus.ERROR]
        assert execution.operation_calls[0]["error"].to_dict()["error_type"] == "RetrievalError"
        session.verify_call.assert_called_once()

    def test_verify_missing_collection_during_search_records_error_before_raising(self) -> None:
        from unittest.mock import MagicMock

        from elspeth.contracts.call_mode import CallModeSession
        from elspeth.contracts.enums import CallStatus, RunMode
        from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalError
        from tests.fixtures.mock_audit import mock_item_audit_authority

        session = MagicMock(spec=CallModeSession)
        session.mode = RunMode.VERIFY
        execution = _ChromaExecutionRecorder()
        provider = self._provider("p3-missing-collection-search", session=session, execution=execution)

        with pytest.raises(RetrievalError, match="does not exist"):
            provider.search("q", 1, 0.0, **mock_item_audit_authority(), state_id="state-1", token_id=None)

        assert [c["status"] for c in execution.recorded_calls] == [CallStatus.ERROR]
        session.verify_call.assert_called_once()


class _FakeAzureCredential:
    def get_token(self, *scopes: str) -> Any:
        raise AssertionError("classification must not acquire a token")


class TestFinding76DataverseRetryAfterCarried:
    """#76: the parsed 429 Retry-After delay is a typed field on DataverseClientError; HTTP-date is parsed."""

    @staticmethod
    def _client() -> Any:
        from elspeth.plugins.infrastructure.clients.dataverse import DataverseClient

        client = DataverseClient(environment_url=_ENV_URL, credential=cast(Any, _FakeAzureCredential()))
        client.close()
        return client

    def test_delta_seconds_retry_after_is_carried(self) -> None:
        error = self._client()._classify_error(429, {"retry-after": "7"}, 1.0)
        assert "retry_after" in vars(error)
        assert error.retryable is True
        assert error.retry_after == 7.0

    def test_http_date_retry_after_is_parsed_not_defaulted(self) -> None:
        from datetime import UTC, datetime, timedelta
        from email.utils import format_datetime

        when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
        error = self._client()._classify_error(429, {"retry-after": when}, 1.0)
        assert "retry_after" in vars(error)
        assert error.retryable is True
        assert 20.0 <= error.retry_after <= 31.0

    def test_http_date_beyond_cap_is_non_retryable(self) -> None:
        from datetime import UTC, datetime, timedelta
        from email.utils import format_datetime

        when = format_datetime(datetime.now(UTC) + timedelta(hours=1), usegmt=True)
        error = self._client()._classify_error(429, {"retry-after": when}, 1.0)
        assert error.retryable is False
        assert "retry_after" in vars(error)
        assert error.retry_after > 3000.0

    def test_absent_retry_after_and_non_throttle_status_carry_none(self) -> None:
        # 503 is a throttle status and carries its Retry-After since #76's
        # consumption landed (test_dataverse_retry_after.py); 500 does not.
        client = self._client()
        missing = client._classify_error(429, {}, 1.0)
        server_error = client._classify_error(500, {"retry-after": "5"}, 1.0)
        assert "retry_after" in vars(missing)
        assert missing.retry_after is None
        assert missing.retryable is True
        assert "retry_after" in vars(server_error)
        assert server_error.retry_after is None


def _sink_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "environment_url": _ENV_URL,
        "auth": _AUTH,
        "entity": "contacts",
        "alternate_key": "emailaddress1",
        "field_mapping": {"email": "emailaddress1", "name": "fullname"},
        "schema": {"mode": "observed"},
    }
    config.update(overrides)
    return config


class TestFinding82UpsertKeyQuoteEscaping:
    """#82: a single quote in an alternate-key value is doubled (OData) before percent-encoding."""

    def test_apostrophe_key_value_is_odata_escaped(self) -> None:
        import urllib.parse

        from elspeth.plugins.sinks.dataverse import DataverseSink

        url = DataverseSink(_sink_config())._build_upsert_url("o'brien@x.com")

        # Servers percent-decode before parsing OData: the decoded key predicate
        # must be one balanced string literal with the quote doubled.
        assert urllib.parse.unquote(url).endswith("/contacts(emailaddress1='o''brien@x.com')")

    def test_key_value_without_quotes_unchanged(self) -> None:
        from elspeth.plugins.sinks.dataverse import DataverseSink

        url = DataverseSink(_sink_config())._build_upsert_url("alice@example.com")
        assert url == f"{_ENV_URL}/api/data/v9.2/contacts(emailaddress1='alice%40example.com')"


class TestFinding85LookupKeysMustBeMapped:
    """#85: a lookup keyed by a pipeline field absent from field_mapping is refused, not silently dropped."""

    def test_unmapped_lookup_key_rejected(self) -> None:
        from elspeth.plugins.sinks.dataverse import DataverseSinkConfig

        config = _sink_config(
            field_mapping={"email": "emailaddress1"},
            lookups={"account_id": {"target_entity": "accounts", "target_field": "parentcustomerid"}},
        )
        with pytest.raises(PluginConfigError, match="account_id"):
            DataverseSinkConfig.from_dict(config)

    def test_mapped_lookup_key_accepted(self) -> None:
        from elspeth.plugins.sinks.dataverse import DataverseSinkConfig

        config = _sink_config(
            field_mapping={"email": "emailaddress1", "account_id": "parentcustomerid"},
            lookups={"account_id": {"target_entity": "accounts", "target_field": "parentcustomerid"}},
        )
        cfg = DataverseSinkConfig.from_dict(config)
        assert cfg.lookups is not None
        assert set(cfg.lookups) == {"account_id"}


def _chroma_sink_config(persist_directory: str, **overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "collection": "p3-sink-collection",
        "mode": "persistent",
        "persist_directory": persist_directory,
        "distance_function": "cosine",
        "field_mapping": {"document_field": "text", "id_field": "doc_id", "metadata_fields": ["topic"]},
        "on_duplicate": "overwrite",
        "schema": {"mode": "fixed", "fields": ["doc_id: str", "text: str", "topic: str"]},
    }
    config.update(overrides)
    return config


class TestFinding92ChromaSinkDistanceFunctionMismatch:
    """#92: on_start refuses an existing collection whose hnsw:space differs from distance_function."""

    def test_existing_collection_with_other_distance_function_refused(self, tmp_path: Any) -> None:
        chromadb = pytest.importorskip("chromadb")
        from elspeth.plugins.sinks.chroma_sink import ChromaSink
        from tests.fixtures.factories import make_context

        persist = str(tmp_path / "chroma")
        chromadb.PersistentClient(path=persist).get_or_create_collection(name="p3-sink-collection", metadata={"hnsw:space": "cosine"})

        sink = ChromaSink(_chroma_sink_config(persist, distance_function="l2"))
        try:
            with pytest.raises(ValueError, match="distance_function"):
                sink.on_start(make_context(node_id="chroma"))
        finally:
            sink.close()

    def test_existing_collection_with_matching_distance_function_accepted(self, tmp_path: Any) -> None:
        chromadb = pytest.importorskip("chromadb")
        from elspeth.plugins.sinks.chroma_sink import ChromaSink
        from tests.fixtures.factories import make_context

        persist = str(tmp_path / "chroma")
        chromadb.PersistentClient(path=persist).get_or_create_collection(name="p3-sink-collection", metadata={"hnsw:space": "l2"})

        sink = ChromaSink(_chroma_sink_config(persist, distance_function="l2"))
        try:
            sink.on_start(make_context(node_id="chroma"))
        finally:
            sink.close()


class TestFinding93ChromaComposerHintMatchesDiversion:
    """#93: the composer hint must say a non-string id/document diverts that row, as the code does."""

    def test_hint_describes_per_row_diversion_for_non_string_id_or_document(self) -> None:
        from elspeth.plugins.sinks.chroma_sink import ChromaSink

        assistance = ChromaSink.get_agent_assistance()
        assert assistance is not None
        hints = " ".join(assistance.composer_hints)
        assert "aborts the whole batch" not in hints
        assert "non-string" in hints
        assert "diverted per row" in hints


def _chroma_member(ordinal: int, row: dict[str, object]) -> Any:
    import hashlib

    from elspeth.contracts.hashing import canonical_json, stable_hash
    from elspeth.contracts.sink_effects import SinkEffectMember

    lineage_json = "[]"
    return SinkEffectMember(
        ordinal=ordinal,
        token_id=f"token-{ordinal}",
        row_id=f"row-{ordinal}",
        ingest_sequence=ordinal,
        lineage_json=lineage_json,
        lineage_hash=hashlib.sha256(lineage_json.encode()).hexdigest(),
        payload_hash=hashlib.sha256(canonical_json(row).encode()).hexdigest(),
        row=row,
        member_effect_id=stable_hash({"effect": "c" * 64, "ordinal": ordinal}),
    )


class TestFinding94ChromaPayloadRejectionDivertsRow:
    """#94: a payload Chroma rejects at commit (ValueError) diverts that row, as the Dataverse sibling does."""

    def test_rejected_member_is_diverted_and_sibling_is_written(self) -> None:
        import uuid
        from datetime import UTC, datetime

        chromadb = pytest.importorskip("chromadb")
        from elspeth.contracts.sink_effects import (
            RestrictedSinkEffectContext,
            SinkEffectInspectionRequest,
            SinkEffectPipelineMembersInput,
            SinkEffectPrepareRequest,
        )
        from elspeth.plugins.sinks.chroma_sink import ChromaSink
        from tests.fixtures.base_classes import inject_write_failure

        collection_name = f"p3-reject-{uuid.uuid4().hex[:12]}"
        collection = chromadb.Client().get_or_create_collection(name=collection_name, metadata={"hnsw:space": "cosine"})
        config = _chroma_sink_config(
            "./unused",
            collection=collection_name,
            field_mapping={"document_field": "text", "id_field": "doc_id", "metadata_fields": ["chroma:document"]},
            schema={"mode": "observed"},
        )
        sink = inject_write_failure(ChromaSink(config))
        sink._collection = collection
        members = (
            _chroma_member(0, {"doc_id": "bad", "text": "rejected", "chroma:document": "reserved key"}),
            _chroma_member(1, {"doc_id": "good", "text": "accepted"}),
        )
        effect_input = SinkEffectPipelineMembersInput(members, members, len(members))
        ctx = RestrictedSinkEffectContext(
            run_id="run-1", run_started_at=datetime(2026, 11, 12, tzinfo=UTC), operation_id="operation-1", sink_node_id="sink-1"
        )
        inspection = sink.inspect_effect(SinkEffectInspectionRequest(effect_id="c" * 64, target="{}", predecessor_descriptor=None), ctx)
        plan = sink.prepare_effect(SinkEffectPrepareRequest(effect_id="c" * 64, effect_input=effect_input, inspection=inspection), ctx)

        outcome: object
        try:
            outcome = sink.commit_member_effect(plan, members[0], effect_input, ctx)
        except Exception as exc:
            outcome = exc
        assert not isinstance(outcome, Exception), f"commit raised {outcome!r} instead of diverting the rejected row"
        rejected = outcome
        accepted = sink.commit_member_effect(plan, members[1], effect_input, ctx)

        assert rejected.diverted_ordinals == (0,)
        assert 0 not in rejected.accepted_ordinals
        assert accepted.diverted_ordinals == ()
        assert collection.get(ids=["good"])["ids"] == ["good"]
        assert collection.get(ids=["bad"])["ids"] == []
