"""#76: every Dataverse retry path honours the server's Retry-After.

DataverseClientError.retry_after (landed with #76's first half) carried the
parsed Retry-After delay, but nothing consumed it: a 429/503 was raised at
once and nothing in the engine retries a source load or a sink member commit,
so throttling either failed the run or retried on no schedule at all.

The client now waits (at least) the server's Retry-After before re-sending a
throttled (429/503) request, bounded by a throttle-wait budget, cancellable
through the run's shutdown event, and observable: the source records each
throttled call with the wait it chose, and the sink's member-commit evidence
names the waits behind a committed member.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest

from elspeth.contracts import CallStatus
from elspeth.contracts.sink_effects import (
    SinkEffectInspectionRequest,
    SinkEffectPipelineMembersInput,
    SinkEffectPrepareRequest,
)
from elspeth.plugins.infrastructure.clients.dataverse import (
    DataverseClient,
    DataverseClientError,
    DataversePageResponse,
)
from tests.fixtures.base_classes import inject_write_failure
from tests.unit.plugins.infrastructure.clients.test_dataverse_client import (
    ENV_URL,
    FakeCredential,
    MockTransport,
    _make_empty_response,
    _make_json_response,
    _make_ssrf_safe,
)
from tests.unit.plugins.sinks.test_dataverse_sink import (
    FakeDataverseRowSchema,
    _effect_member,
    _restricted_effect_context,
)
from tests.unit.plugins.sinks.test_dataverse_sink import _config as _sink_config
from tests.unit.plugins.sources.test_dataverse_source import (
    _base_config,
    _client_secret_credential_factory,
    _LifecycleContextFake,
    _SourceContextFake,
)

_URL = f"{ENV_URL}/api/data/v9.2/accounts"
_SSRF_PATCH = "elspeth.plugins.infrastructure.clients.dataverse.validate_url_for_ssrf"


class _RecordingShutdownEvent:
    """A never-set shutdown event that records each wait instead of sleeping."""

    def __init__(self, *, fire: bool = False) -> None:
        self.waits: list[float] = []
        self._fire = fire

    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None
        self.waits.append(timeout)
        return self._fire


def _assert_throttle_api() -> None:
    params = inspect.signature(DataverseClient.__init__).parameters
    assert "throttle_wait_budget_seconds" in params, "DataverseClient has no throttle-wait budget"
    assert "shutdown_event" in params, "DataverseClient cannot be cancelled during a throttle wait"
    assert "on_throttle" in params, "DataverseClient exposes no throttle observer for audit"
    assert "throttle_waits" in {f.name for f in dataclasses.fields(DataversePageResponse)}


def _client(transport: MockTransport, event: _RecordingShutdownEvent, **kwargs: Any) -> DataverseClient:
    _assert_throttle_api()
    client = DataverseClient(
        environment_url=ENV_URL,
        credential=cast(Any, FakeCredential()),
        shutdown_event=cast(Any, event),
        **kwargs,
    )
    client._client.close()
    client._client = httpx.Client(transport=transport, timeout=30.0)
    return client


def _throttled(status: int, retry_after: str | None) -> httpx.Response:
    return httpx.Response(status_code=status, headers={} if retry_after is None else {"retry-after": retry_after}, text="")


@pytest.fixture(autouse=True)
def _bypass_ssrf_dns() -> Any:
    with patch(_SSRF_PATCH, side_effect=lambda url, **_kwargs: _make_ssrf_safe(url)):
        yield


class TestClientHonoursRetryAfter:
    def test_429_waits_retry_after_then_resends(self) -> None:
        event = _RecordingShutdownEvent()
        transport = MockTransport([_throttled(429, "7"), _make_json_response({"value": [{"id": "1"}]})])
        page = _client(transport, event).get_page(_URL)
        assert event.waits == [7.0]
        assert len(transport.requests) == 2
        assert [dict(row) for row in page.rows] == [{"id": "1"}]
        assert page.throttle_waits == (7.0,)

    def test_503_retry_after_is_parsed_and_honoured(self) -> None:
        event = _RecordingShutdownEvent()
        transport = MockTransport([_throttled(503, "3"), _make_json_response({"value": []})])
        page = _client(transport, event).get_page(_URL)
        assert event.waits == [3.0]
        assert page.throttle_waits == (3.0,)

    def test_503_classification_carries_retry_after(self) -> None:
        _assert_throttle_api()
        client = DataverseClient(environment_url=ENV_URL, credential=cast(Any, FakeCredential()))
        error = client._classify_error(503, {"retry-after": "5"}, 1.0)
        client.close()
        assert error.retryable is True
        assert error.retry_after == 5.0

    def test_429_without_retry_after_still_backs_off(self) -> None:
        event = _RecordingShutdownEvent()
        transport = MockTransport([_throttled(429, None), _make_json_response({"value": []})])
        _client(transport, event).get_page(_URL)
        assert len(event.waits) == 1
        assert event.waits[0] >= 1.0

    def test_retry_after_beyond_budget_fails_retryable_without_sleeping(self) -> None:
        event = _RecordingShutdownEvent()
        transport = MockTransport([_throttled(429, "30")])
        with pytest.raises(DataverseClientError) as exc_info:
            _client(transport, event, throttle_wait_budget_seconds=10.0).get_page(_URL)
        assert event.waits == []
        assert len(transport.requests) == 1
        assert exc_info.value.retryable is True
        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after == 30.0

    def test_budget_is_spent_across_waits(self) -> None:
        event = _RecordingShutdownEvent()
        transport = MockTransport([_throttled(429, "6"), _throttled(429, "6")])
        with pytest.raises(DataverseClientError) as exc_info:
            _client(transport, event, throttle_wait_budget_seconds=10.0).get_page(_URL)
        assert event.waits == [6.0]
        assert len(transport.requests) == 2
        assert exc_info.value.retryable is True
        assert exc_info.value.throttle_waits == (6.0,)
        assert "budget" in str(exc_info.value)

    def test_observer_sees_each_waited_response_before_the_resend(self) -> None:
        # Observed after the wait completes and before the re-send, so a wait
        # cut short by shutdown leaves the throttled call to the caller's
        # error path (recorded exactly once) instead of recording it twice.
        order: list[str] = []
        event = _RecordingShutdownEvent()
        seen: list[tuple[int | None, float | None, float]] = []
        transport = MockTransport([_throttled(429, "2"), _throttled(503, "4"), _make_json_response({"value": []})])

        def observe(error: DataverseClientError, wait: float) -> None:
            order.append(f"waits={len(event.waits)},sent={len(transport.requests)}")
            seen.append((error.status_code, error.retry_after, wait))

        page = _client(transport, event, on_throttle=observe).get_page(_URL)
        assert seen == [(429, 2.0, 2.0), (503, 4.0, 4.0)]
        assert order == ["waits=1,sent=1", "waits=2,sent=2"]
        assert event.waits == [2.0, 4.0]
        assert page.throttle_waits == (2.0, 4.0)

    def test_upsert_honours_retry_after(self) -> None:
        event = _RecordingShutdownEvent()
        transport = MockTransport([_throttled(429, "5"), _make_empty_response(204)])
        page = _client(transport, event).upsert(f"{ENV_URL}/api/data/v9.2/contacts(emailaddress1='a')", {"fullname": "A"})
        assert event.waits == [5.0]
        assert [request.method for request in transport.requests] == ["PATCH", "PATCH"]
        assert page.throttle_waits == (5.0,)

    def test_non_throttle_server_error_is_not_retried_in_client(self) -> None:
        event = _RecordingShutdownEvent()
        transport = MockTransport([_throttled(500, "5")])
        with pytest.raises(DataverseClientError) as exc_info:
            _client(transport, event).get_page(_URL)
        assert exc_info.value.status_code == 500
        assert event.waits == []
        assert len(transport.requests) == 1

    def test_shutdown_during_wait_stops_retrying(self) -> None:
        event = _RecordingShutdownEvent(fire=True)
        observed: list[float] = []
        transport = MockTransport([_throttled(429, "5")])
        with pytest.raises(DataverseClientError) as exc_info:
            _client(transport, event, on_throttle=lambda _error, wait: observed.append(wait)).get_page(_URL)
        assert event.waits == [5.0]
        assert observed == []
        assert len(transport.requests) == 1
        assert exc_info.value.retryable is True
        assert "shutdown" in str(exc_info.value)


class TestSourceRecordsThrottledCalls:
    def test_throttled_metadata_call_is_recorded_with_its_wait(self) -> None:
        _assert_throttle_api()
        from elspeth.plugins.sources.dataverse import DataverseSource

        event = _RecordingShutdownEvent()
        source = DataverseSource(_base_config())
        with patch("azure.identity.ClientSecretCredential", new=_client_secret_credential_factory):
            source.on_start(cast(Any, _LifecycleContextFake(shutdown_event=event)))
        transport = MockTransport(
            [
                _throttled(429, "3"),
                _make_json_response({"LogicalName": "contact", "EntitySetName": "contacts"}),
                _make_json_response({"value": [{"contactid": "1"}]}),
            ]
        )
        source._client._client.close()
        source._client._client = httpx.Client(transport=transport, timeout=30.0)
        ctx = _SourceContextFake()

        rows = list(source.load(cast(Any, ctx)))

        assert len(rows) == 1
        assert event.waits == [3.0]
        statuses = [call.kwargs["status"] for call in ctx.record_call.call_args_list]
        assert statuses == [CallStatus.ERROR, CallStatus.SUCCESS, CallStatus.SUCCESS]
        throttled = ctx.record_call.call_args_list[0].kwargs["error"]
        assert throttled["status_code"] == 429
        assert throttled["retry_wait_ms"] == 3000
        source.close()


class TestSinkCommitEvidenceNamesThrottleWaits:
    @patch("elspeth.plugins.sinks.dataverse.create_schema_from_config", return_value=FakeDataverseRowSchema)
    def test_committed_member_evidence_carries_waits(self, _mock_schema: Any) -> None:
        _assert_throttle_api()
        from elspeth.plugins.sinks.dataverse import DataverseSink

        event = _RecordingShutdownEvent()
        transport = MockTransport([_throttled(429, "2"), _make_empty_response(204)])
        sink = inject_write_failure(DataverseSink(_sink_config()))
        sink._client = _client(transport, event)
        member = _effect_member(0, {"email": "a", "name": "Alice"})
        effect_input = SinkEffectPipelineMembersInput((member,), (member,), 1)
        ctx = _restricted_effect_context()
        inspection = sink.inspect_effect(SinkEffectInspectionRequest(effect_id="a" * 64, target="{}", predecessor_descriptor=None), ctx)
        plan = sink.prepare_effect(SinkEffectPrepareRequest(effect_id="a" * 64, effect_input=effect_input, inspection=inspection), ctx)

        result = sink.commit_member_effect(plan, member, effect_input, ctx)

        assert event.waits == [2.0]
        assert result.evidence["classification"] == "committed"
        assert list(result.evidence["throttle_wait_ms"]) == [2000]

    def test_on_start_passes_the_run_shutdown_event(self) -> None:
        _assert_throttle_api()
        from elspeth.plugins.sinks.dataverse import DataverseSink

        captured: dict[str, Any] = {}

        def factory(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return object()

        event = _RecordingShutdownEvent()
        with (
            patch("elspeth.plugins.sinks.dataverse.create_schema_from_config", return_value=FakeDataverseRowSchema),
            patch("azure.identity.ClientSecretCredential", new=_client_secret_credential_factory),
            patch("elspeth.plugins.sinks.dataverse.DataverseClient", new=factory),
        ):
            sink = DataverseSink(_sink_config())
            sink.on_start(cast(Any, _LifecycleContextFake(shutdown_event=event)))
        assert captured["shutdown_event"] is event
