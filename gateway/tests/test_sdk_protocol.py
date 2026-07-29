import pytest
from elspeth_llm_gateway.sdk.protocol import (
    CLASSIFIABLE_CODES,
    AdapterDescriptor,
    AdapterProtocol,
    ErrorClassification,
    InvokePlan,
    UpstreamFailure,
)
from elspeth_llm_gateway.sdk.types import CanonicalRequest, CanonicalResponse, Capability
from pydantic import ValidationError

# --- AdapterDescriptor -------------------------------------------------------


def test_adapter_descriptor_happy_path():
    descriptor = AdapterDescriptor(
        name="openai_chat",
        version="1.0.0",
        adapter_api_major=1,
        capabilities=frozenset({Capability.TEXT, Capability.TOOLS}),
    )
    assert descriptor.name == "openai_chat"
    assert Capability.TEXT in descriptor.capabilities


def test_adapter_descriptor_requires_text_capability():
    with pytest.raises(ValidationError):
        AdapterDescriptor(
            name="openai_chat",
            version="1.0.0",
            adapter_api_major=1,
            capabilities=frozenset({Capability.TOOLS}),
        )


@pytest.mark.parametrize(
    "name",
    [
        "AB",  # uppercase, too short
        "ab",  # too short (< 3 chars)
        "1abc",  # must start with a-z
        "_abc",  # must start with a-z
        "Abcdef",  # uppercase not allowed
        "abc-def",  # hyphen not allowed
        "a" * 65,  # too long (max total length 64: 1 + {2,63})
        "abc\n",  # trailing newline must not slip past a naive $-anchored regex
    ],
)
def test_adapter_descriptor_rejects_bad_name(name):
    with pytest.raises(ValidationError):
        AdapterDescriptor(
            name=name,
            version="1.0.0",
            adapter_api_major=1,
            capabilities=frozenset({Capability.TEXT}),
        )


def test_adapter_descriptor_accepts_boundary_names():
    AdapterDescriptor(
        name="abc",
        version="1.0.0",
        adapter_api_major=1,
        capabilities=frozenset({Capability.TEXT}),
    )
    AdapterDescriptor(
        name="a" * 64,
        version="1.0.0",
        adapter_api_major=1,
        capabilities=frozenset({Capability.TEXT}),
    )


def test_adapter_descriptor_is_frozen():
    descriptor = AdapterDescriptor(
        name="openai_chat",
        version="1.0.0",
        adapter_api_major=1,
        capabilities=frozenset({Capability.TEXT}),
    )
    with pytest.raises(ValidationError):
        descriptor.name = "other"


def test_adapter_descriptor_forbids_extra():
    with pytest.raises(ValidationError):
        AdapterDescriptor(
            name="openai_chat",
            version="1.0.0",
            adapter_api_major=1,
            capabilities=frozenset({Capability.TEXT}),
            extra_field=1,
        )


# --- InvokePlan: path validation ---------------------------------------------


def test_invoke_plan_happy_path():
    plan = InvokePlan(path="v1/chat/completions", headers={}, body={"a": 1})
    assert plan.path == "v1/chat/completions"
    assert plan.body == {"a": 1}


@pytest.mark.parametrize(
    "path",
    [
        "/abs",
        "a/../b",
        "https://evil",
        "http://evil",
        "foo://bar",
        "..",
        "a/../../b",
        "a?x=1",
        "a#frag",
        "a b",
        "a\tb",
        "a\nb",
    ],
)
def test_invoke_plan_rejects_dangerous_paths(path):
    with pytest.raises(ValidationError):
        InvokePlan(path=path, headers={}, body={})


# --- InvokePlan: header validation --------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "x"},
        {"AUTHORIZATION": "x"},
        {"authorization": "x"},
        {"Host": "evil.example"},
        {"HOST": "evil.example"},
        {"Cookie": "a=b"},
        {"X-Forwarded-For": "1.2.3.4"},
        {"x-forwarded-for": "1.2.3.4"},
    ],
)
def test_invoke_plan_rejects_forbidden_headers(headers):
    with pytest.raises(ValidationError):
        InvokePlan(path="v1/chat", headers=headers, body={})


def test_invoke_plan_normalizes_header_names_to_lowercase():
    plan = InvokePlan(path="v1/chat", headers={"X-Custom-Header": "value"}, body={})
    assert plan.headers == {"x-custom-header": "value"}


def test_invoke_plan_rejects_case_colliding_header_names():
    with pytest.raises(ValidationError):
        InvokePlan(path="v1/chat", headers={"X-Foo": "a", "x-foo": "b"}, body={})


def test_invoke_plan_rejects_oversized_header_value():
    with pytest.raises(ValidationError):
        InvokePlan(path="v1/chat", headers={"x-custom": "a" * 1025}, body={})


def test_invoke_plan_accepts_boundary_header_value_length():
    plan = InvokePlan(path="v1/chat", headers={"x-custom": "a" * 1024}, body={})
    assert plan.headers["x-custom"] == "a" * 1024


def test_invoke_plan_default_headers_is_empty_dict():
    plan = InvokePlan(path="v1/chat", body={})
    assert plan.headers == {}


def test_invoke_plan_is_frozen():
    plan = InvokePlan(path="v1/chat", headers={}, body={})
    with pytest.raises(ValidationError):
        plan.path = "other"


def test_invoke_plan_forbids_extra():
    with pytest.raises(ValidationError):
        InvokePlan(path="v1/chat", headers={}, body={}, extra_field=1)


# --- UpstreamFailure ----------------------------------------------------------


def test_upstream_failure_happy_path():
    failure = UpstreamFailure(status=429, body={"error": "rate_limited"})
    assert failure.status == 429
    assert failure.body == {"error": "rate_limited"}


def test_upstream_failure_body_may_be_none():
    failure = UpstreamFailure(status=500, body=None)
    assert failure.body is None


def test_upstream_failure_is_frozen():
    failure = UpstreamFailure(status=500, body=None)
    with pytest.raises(ValidationError):
        failure.status = 400


def test_upstream_failure_forbids_extra():
    with pytest.raises(ValidationError):
        UpstreamFailure(status=500, body=None, extra_field=1)


# --- ErrorClassification / CLASSIFIABLE_CODES --------------------------------


def test_classifiable_codes_literal_value():
    assert {
        "context_length_exceeded",
        "content_policy_rejected",
        "upstream_rate_limited",
        "upstream_timeout",
        "upstream_unavailable",
        "upstream_response_invalid",
    } == CLASSIFIABLE_CODES


def test_classifiable_codes_is_frozenset():
    assert isinstance(CLASSIFIABLE_CODES, frozenset)


@pytest.mark.parametrize("code", sorted(CLASSIFIABLE_CODES))
def test_error_classification_accepts_classifiable_codes(code):
    classification = ErrorClassification(code=code, retryable=True)
    assert classification.code == code


def test_error_classification_rejects_non_classifiable_code():
    with pytest.raises(ValidationError):
        ErrorClassification(code="internal_error", retryable=False)


def test_error_classification_is_frozen():
    classification = ErrorClassification(code="upstream_timeout", retryable=True)
    with pytest.raises(ValidationError):
        classification.retryable = False


def test_error_classification_forbids_extra():
    with pytest.raises(ValidationError):
        ErrorClassification(code="upstream_timeout", retryable=True, extra_field=1)


# --- AdapterProtocol ----------------------------------------------------------


def test_adapter_protocol_is_runtime_checkable():
    class ConformingAdapter:
        def descriptor(self) -> AdapterDescriptor:
            return AdapterDescriptor(
                name="conforming",
                version="1.0.0",
                adapter_api_major=1,
                capabilities=frozenset({Capability.TEXT}),
            )

        def validate_configuration(self, options: dict) -> None:
            return None

        def build_invoke(self, request: CanonicalRequest) -> InvokePlan:
            return InvokePlan(path="v1/chat", headers={}, body={})

        def parse_success(self, body: dict) -> CanonicalResponse:
            raise NotImplementedError

        def classify_error(self, failure: UpstreamFailure) -> ErrorClassification:
            return ErrorClassification(code="upstream_timeout", retryable=True)

    assert isinstance(ConformingAdapter(), AdapterProtocol)


def test_adapter_protocol_rejects_non_conforming_object():
    class NotAnAdapter:
        pass

    assert not isinstance(NotAnAdapter(), AdapterProtocol)
