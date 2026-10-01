"""Nominal, immutable transport authority for member sink effects."""

from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from elspeth.contracts.sink_effects import RestrictedSinkEffectContext, SinkEffectExecutionPurpose, SinkEffectRuntimeBinding


def test_post_request_freezes_owned_json_without_url_authority():
    from elspeth.contracts.sink_effect_http import SinkEffectHTTPPostRequest

    original = {"data": {"nested": [1, 2]}}
    request = SinkEffectHTTPPostRequest(json_body=original)
    original["data"]["nested"].append(3)
    assert isinstance(request.json_body, MappingProxyType)
    assert request.json_body["data"]["nested"] == (1, 2)
    with pytest.raises(TypeError):
        request.json_body["url"] = "https://other.example"
    with pytest.raises(ValueError, match="finite"):
        SinkEffectHTTPPostRequest(json_body={"data": float("nan")})


def test_response_requires_actual_call_and_payload_references():
    from elspeth.contracts.sink_effect_http import SinkEffectHTTPPostResponse

    response = SinkEffectHTTPPostResponse(
        status_code=200,
        content_type="application/json",
        body=b"{}",
        call_id="call-1",
        request_ref="a" * 64,
        response_ref="b" * 64,
    )
    assert response.body == b"{}"
    with pytest.raises(ValueError, match="request_ref"):
        SinkEffectHTTPPostResponse(
            status_code=200,
            content_type=None,
            body=b"{}",
            call_id="call-1",
            request_ref="",
            response_ref="b" * 64,
        )
    with pytest.raises(TypeError, match="status_code"):
        SinkEffectHTTPPostResponse(
            status_code=True,
            content_type=None,
            body=b"{}",
            call_id="call-1",
            request_ref="a" * 64,
            response_ref="b" * 64,
        )


def test_runtime_binding_refuses_structural_factory_impostor():
    class Impostor:
        def bind(self, context):
            return context

    sink = object()
    with pytest.raises(TypeError, match="must be a nominal SinkEffectHTTPPostFactory"):
        SinkEffectRuntimeBinding(
            sink_name="output",
            sink=sink,
            sink_type=object,
            config_fingerprint="a" * 64,
            purpose=SinkEffectExecutionPurpose.FRESH,
            effect_mode=None,
            http_post_factory=Impostor(),
        )


def test_restricted_context_refuses_structural_http_impostor():
    class Impostor:
        def post_json(self, request):
            return request

    with pytest.raises(TypeError, match="must be a nominal SinkEffectHTTPPost"):
        RestrictedSinkEffectContext(
            run_id="run-1",
            run_started_at=datetime.now(UTC),
            operation_id="op-1",
            sink_node_id="sink-1",
            http_post=Impostor(),
        )


def test_existing_non_http_context_has_no_transport():
    ctx = RestrictedSinkEffectContext(run_id="run-1", run_started_at=datetime.now(UTC), operation_id="op-1", sink_node_id="sink-1")
    assert ctx.http_post is None


def test_nominal_http_factory_and_capability_are_accepted_without_binding():
    from elspeth.contracts.sink_effect_http import SinkEffectHTTPPost, SinkEffectHTTPPostFactory

    class Post(SinkEffectHTTPPost):
        def post_json(self, request):
            raise AssertionError("construction must not dispatch")

    class Factory(SinkEffectHTTPPostFactory):
        @property
        def safe_config_fingerprint(self):
            return "a" * 64

        def bind(self, context):
            raise AssertionError("construction must not bind")

    sink = object()
    factory = Factory()
    binding = SinkEffectRuntimeBinding(
        sink_name="output",
        sink=sink,
        sink_type=object,
        config_fingerprint="a" * 64,
        purpose=SinkEffectExecutionPurpose.FRESH,
        effect_mode=None,
        http_post_factory=factory,
    )
    post = Post()
    ctx = RestrictedSinkEffectContext(
        run_id="run-1",
        run_started_at=datetime.now(UTC),
        operation_id="op-1",
        sink_node_id="sink-1",
        http_post=post,
    )
    assert binding.http_post_factory is factory
    assert ctx.http_post is post
