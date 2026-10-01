"""Closed Power Automate configuration and protocol boundaries."""

import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import stable_hash
from elspeth.plugins.infrastructure.power_automate import (
    PROTOCOL,
    PowerAutomateProtocolError,
    PowerAutomateSinkConfig,
    PowerAutomateSourceConfig,
    parse_effect_response,
    parse_read_response,
    selected_data_hash,
    validate_trigger_url,
)

ORIGIN = "https://flow.example.org"
URL = ORIGIN + "/a%2Fb?api-version=1&sig=a%252Fb"
DELIVERY = "b" * 64
PAYLOAD = "a" * 64


def source_options(**extra):
    return {
        "schema": {"mode": "observed"},
        "auth": {"method": "sas_url", "trigger_url_secret": URL},
        "allowed_origin": ORIGIN,
        "on_validation_failure": "discard",
        **extra,
    }


def sink_options(**extra):
    return {
        "schema": {"mode": "flexible", "fields": ["record_id: str"]},
        "auth": {"method": "managed_identity", "client_id": "user-id"},
        "trigger_url": ORIGIN + "/trigger",
        "allowed_origin": ORIGIN,
        "fields": ["record_id"],
        **extra,
    }


def read_bytes(**extra):
    return json.dumps({"protocol": PROTOCOL, "snapshot_id": "s1", "rows": [], "next_cursor": None, **extra}).encode()


def effect_bytes(state="applied", **extra):
    fields = {"receipt_id": "r1", "flow_run_id": "f1"} if state == "applied" else {}
    return json.dumps(
        {"protocol": PROTOCOL, "delivery_id": DELIVERY, "payload_sha256": PAYLOAD, "state": state, **fields, **extra}
    ).encode()


def parse_effect(body, operation="status"):
    return parse_effect_response(body, expected_delivery_id=DELIVERY, expected_payload_sha256=PAYLOAD, operation=operation)


def test_configs_defaults_and_private_secrets():
    cfg = PowerAutomateSourceConfig.model_validate(source_options())
    assert isinstance(cfg.auth.trigger_url_secret, SecretStr)
    assert cfg.auth.trigger_url_secret.get_secret_value() == URL
    assert "a%252Fb" not in repr(cfg)
    assert (cfg.page_size, cfg.max_pages, cfg.max_rows) == (100, 1000, 100000)
    assert cfg.snapshot_for_resume is False
    assert cfg.timeout_seconds == 90
    assert cfg.max_response_body_bytes == 4194304
    assert PowerAutomateSinkConfig.model_validate(sink_options()).max_response_body_bytes == 1048576
    with pytest.raises(ValidationError):
        cfg.page_size = 10


@pytest.mark.parametrize(
    "method,auth",
    [
        ("sas_url", {"method": "sas_url", "trigger_url_secret": URL}),
        ("service_principal", {"method": "service_principal", "tenant_id": "tenant", "client_id": "client", "client_secret": "secret"}),
        ("managed_identity", {"method": "managed_identity", "client_id": "client"}),
    ],
)
def test_auth_valid_variants(method, auth):
    opts = source_options(auth=auth)
    if method != "sas_url":
        opts["trigger_url"] = ORIGIN + "/trigger?api-version=1"
    assert PowerAutomateSourceConfig.model_validate(opts).auth.method == method


@pytest.mark.parametrize(
    "auth",
    [
        {"method": "managed_identity"},
        {"method": "service_principal", "client_id": "c"},
        {"method": "sas_url", "trigger_url_secret": URL, "client_id": "c"},
        {"method": "managed_identity", "client_id": "c", "client_secret": "secret"},
        {"method": "service_principal", "tenant_id": "t", "client_id": "c", "client_secret": ""},
        {"method": "service_principal", "tenant_id": "t", "client_id": "c", "client_secret": "s", "scope": "other"},
        {"method": "unknown"},
    ],
)
def test_auth_invalid_variants(auth):
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(source_options(auth=auth))


@pytest.mark.parametrize(
    "extra",
    [
        {"trigger_url": ORIGIN + "/other"},
        {"page_size": True},
        {"page_size": "1"},
        {"page_size": 1001},
        {"page_size": 0},
        {"max_pages": False},
        {"max_rows": 0},
        {"max_request_body_bytes": True},
        {"max_response_body_bytes": 0},
        {"timeout_seconds": True},
        {"timeout_seconds": "90"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 111},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"snapshot_for_resume": 1},
        {"query": []},
        {"query": {"x": "${CREDENTIAL}"}},
        {"query": {"token": "value"}},
        {"query": {"nested": {"secret_ref": "locator"}}},
        {"query": {"x": float("nan")}},
        {"snapshot_id": ""},
        {"snapshot_id": "é" * 129},
        {"unknown": 1},
    ],
)
def test_strict_source_configuration(extra):
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(source_options(**extra))


@pytest.mark.parametrize("fields", [[], ["record_id", "record_id"], ["missing"], ["Record Id"], ["record_id", 1]])
def test_sink_fields_are_unique_declared_required(fields):
    with pytest.raises(ValidationError):
        PowerAutomateSinkConfig.model_validate(sink_options(fields=fields))


def test_optional_sink_field_does_not_satisfy_required_input():
    with pytest.raises(ValidationError):
        PowerAutomateSinkConfig.model_validate(sink_options(schema={"mode": "flexible", "fields": ["record_id: str?"]}))


def test_source_requires_route_and_reachable_field_names():
    opts = source_options()
    del opts["on_validation_failure"]
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(opts)
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(source_options(schema={"mode": "fixed", "fields": ["RecordId: str"]}))
    cfg = PowerAutomateSourceConfig.model_validate(
        source_options(schema={"mode": "fixed", "fields": ["RecordId: str"]}, field_mapping={"recordid": "RecordId"})
    )
    assert cfg.field_mapping == {"recordid": "RecordId"}


@pytest.mark.parametrize(
    "origin",
    [
        "http://flow.example.org",
        ORIGIN + "/",
        ORIGIN + "/path",
        ORIGIN + "?x=1",
        ORIGIN + "#x",
        "https://*.example.org",
        "https://user@flow.example.org",
        ORIGIN + ":444",
    ],
)
def test_origin_is_exact_https443_origin(origin):
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(source_options(allowed_origin=origin))


@pytest.mark.parametrize(
    "url",
    [
        "http://flow.example.org/x?sig=x",
        ORIGIN + "/x?sig=",
        ORIGIN + "/x",
        ORIGIN + "/x?sig=x#f",
        "https://user@flow.example.org/x?sig=x",
        "https://other.example.org/x?sig=x",
        ORIGIN + ":444/x?sig=x",
        ORIGIN + "/" + "a" * 16384 + "?sig=x",
        " " + URL,
    ],
)
def test_sas_url_failures_are_value_free(url):
    with pytest.raises(ValueError) as caught:
        validate_trigger_url(url, allowed_origin=ORIGIN, sas=True)
    assert url not in str(caught.value)


def test_url_preserves_percent_encoding_and_normalizes_origin():
    cfg = PowerAutomateSourceConfig.model_validate(source_options(allowed_origin="https://FLOW.example.org:443"))
    assert cfg.allowed_origin == ORIGIN
    assert validate_trigger_url(URL, allowed_origin=cfg.allowed_origin, sas=True) == URL


@pytest.mark.parametrize("query", ["sig=x", "access_token=x", "client_secret=x", "api_key=x", "code=x", "token=x", "%73ig=x"])
def test_oauth_urls_reject_credential_query(query):
    with pytest.raises(ValueError):
        validate_trigger_url(ORIGIN + "/x?" + query, allowed_origin=ORIGIN, sas=False)


def test_read_empty_page_and_arbitrary_candidates_are_frozen():
    assert parse_read_response(read_bytes(), page_size=100).rows == ()
    rows = [{"x": [1]}, None, 1, "value", [2], True]
    page = parse_read_response(read_bytes(rows=rows), page_size=100)
    assert deep_thaw(page.rows) == rows
    assert isinstance(page.rows[0], Mapping)
    with pytest.raises(TypeError):
        page.rows[0]["x"] = 1
    with pytest.raises(FrozenInstanceError):
        page.snapshot_id = "changed"


@pytest.mark.parametrize(
    "extra",
    [
        {"protocol": "other"},
        {"snapshot_id": None},
        {"snapshot_id": ""},
        {"rows": {}},
        {"rows": [1, 2]},
        {"next_cursor": ""},
        {"next_cursor": 1},
        {"next_cursor": "é" * 2049},
        {"unexpected": "secret-value"},
    ],
)
def test_read_closed_envelope(extra):
    with pytest.raises(PowerAutomateProtocolError):
        parse_read_response(read_bytes(**extra), page_size=1)


@pytest.mark.parametrize(
    "body,code",
    [
        (b'{"snapshot_id":"s1","snapshot_id":"secret-value"}', "duplicate_key"),
        (b'{"x":NaN}', "non_finite"),
        (b'{"x":Infinity}', "non_finite"),
        (b'{"x":1e999}', "non_finite"),
        (b"[]", "invalid_envelope"),
        (b"not-json-secret-value", "invalid_json"),
        (b"\xff", "invalid_encoding"),
        (b"[" * 65 + b"0" + b"]" * 65, "depth_exceeded"),
    ],
)
def test_json_boundary_codes_contain_no_external_values(body, code):
    with pytest.raises(PowerAutomateProtocolError, match=code) as caught:
        parse_read_response(body, page_size=100)
    assert caught.value.code == code
    assert str(caught.value) == code
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("state", ["applied", "not_applied", "unknown", "rejected"])
def test_effect_exact_variants(state):
    extra = {"reason_code": "policy_denied"} if state == "rejected" else {}
    response = parse_effect(effect_bytes(state, **extra))
    assert response.state == state
    assert response.delivery_id == DELIVERY
    assert response.payload_sha256 == PAYLOAD
    with pytest.raises(FrozenInstanceError):
        response.delivery_id = "c" * 64


@pytest.mark.parametrize(
    "state,extra",
    [
        ("applied", {"receipt_id": ""}),
        ("applied", {"receipt_id": "é" * 129}),
        ("applied", {"flow_run_id": None}),
        ("applied", {"reason_code": "validation_failed"}),
        ("unknown", {"receipt_id": "r"}),
        ("not_applied", {"flow_run_id": "r"}),
        ("rejected", {}),
        ("rejected", {"reason_code": "untrusted"}),
        ("rejected", {"reason_code": "target_conflict", "receipt_id": "r"}),
        ("pending", {}),
        ("applied", {"delivery_id": "C" * 64}),
        ("applied", {"delivery_id": "c" * 64}),
        ("applied", {"payload_sha256": "c" * 64}),
        ("applied", {"protocol": "other"}),
        ("applied", {"extra": 1}),
    ],
)
def test_effect_invalid_variants_and_identities(state, extra):
    with pytest.raises(PowerAutomateProtocolError):
        parse_effect(effect_bytes(state, **extra))


def test_write_cannot_prove_safe_absence():
    with pytest.raises(PowerAutomateProtocolError):
        parse_effect(effect_bytes("not_applied"), operation="write")


def test_selected_data_hash_is_core_canonical_hash():
    assert selected_data_hash({"b": [2, 1], "a": "é"}) == stable_hash({"a": "é", "b": [2, 1]})
    assert selected_data_hash({"x": 1}) != selected_data_hash({"x": 2})


def test_constructors_perform_no_io(monkeypatch):
    import socket

    def deny(*args, **kwargs):
        raise AssertionError("constructor performed I/O")

    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket, "socket", deny)
    PowerAutomateSourceConfig.model_validate(source_options())
    PowerAutomateSinkConfig.model_validate(sink_options())


def test_config_nested_options_are_detached_and_frozen():
    query = {"filters": [{"dataset": "records"}]}
    mapping = {"record_id": "RecordId"}
    cfg = PowerAutomateSourceConfig.model_validate(source_options(query=query, field_mapping=mapping))
    query["filters"][0]["dataset"] = "changed"
    mapping["record_id"] = "changed"
    assert cfg.query["filters"][0]["dataset"] == "records"
    assert cfg.field_mapping["record_id"] == "RecordId"
    with pytest.raises(TypeError):
        cfg.query["filters"][0]["dataset"] = "changed"


@pytest.mark.parametrize("mapping", [{"Record Id": "record_id"}, {"RecordId": "record_id"}, {"x": "a", "y": "a"}])
def test_field_mapping_rejects_unreachable_keys_and_collisions(mapping):
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(source_options(field_mapping=mapping))


def test_query_and_response_depth_controls_ignore_quoted_brackets():
    assert parse_read_response(read_bytes(rows=[{"text": "[" * 100 + '\\"' + "]" * 100}]), page_size=100).rows
    query = {}
    cursor = query
    for _ in range(65):
        cursor["child"] = {}
        cursor = cursor["child"]
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(source_options(query=query))


def test_source_query_body_budget_is_measured_utf8():
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(source_options(query={"name": "é" * 20}, max_request_body_bytes=30))


@pytest.mark.parametrize("reason", ["validation_failed", "policy_denied", "target_conflict"])
def test_rejection_closed_reason_codes(reason):
    assert parse_effect(effect_bytes("rejected", reason_code=reason), "write").reason_code == reason


def test_response_duplicate_nested_row_key_rejects_entire_page():
    body = b'{"protocol":"elspeth.power-automate.v1","snapshot_id":"s","rows":[{"x":1,"x":2}],"next_cursor":null}'
    with pytest.raises(PowerAutomateProtocolError, match="duplicate_key"):
        parse_read_response(body, page_size=100)


def test_wire_json_schemas_match_closed_response_shapes():
    from jsonschema import Draft202012Validator

    root = Path(__file__).resolve().parents[4]
    schema = json.loads((root / "examples/power_automate/contracts/response.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for response in [
        read_bytes(rows=[None, 1, [], True]),
        effect_bytes(),
        effect_bytes("unknown"),
        effect_bytes("not_applied"),
        effect_bytes("rejected", reason_code="validation_failed"),
    ]:
        validator.validate(json.loads(response))
    for response in [read_bytes(extra=1), effect_bytes("unknown", receipt_id="r"), effect_bytes("rejected", reason_code="invented")]:
        assert list(validator.iter_errors(json.loads(response)))
    for name, operation in [("read", "read"), ("write", "write"), ("status", "status")]:
        contract = json.loads((root / f"examples/power_automate/contracts/{name}.schema.json").read_text())
        Draft202012Validator.check_schema(contract)
        assert contract["properties"]["operation"] == {"const": operation}
        assert contract["additionalProperties"] is False


@pytest.mark.parametrize("name", ["x-auth-token", "ocp-apim-subscription-key", "credential_value", "pwd"])
def test_query_and_url_reject_shared_sensitive_names(name):
    with pytest.raises(ValueError):
        validate_trigger_url(ORIGIN + "/trigger?" + name + "=value", allowed_origin=ORIGIN, sas=False)
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(source_options(query={"nested": {name: "value"}}))


def test_oversized_integer_timeout_is_configuration_error():
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(source_options(timeout_seconds=10**1000))


def schema_validator(name):
    from jsonschema import Draft202012Validator

    root = Path(__file__).resolve().parents[4]
    return Draft202012Validator(json.loads((root / f"examples/power_automate/contracts/{name}.schema.json").read_text()))


@pytest.mark.parametrize("state", ["applied", "not_applied", "unknown", "rejected"])
@pytest.mark.parametrize(
    "field,value",
    [("delivery_id", DELIVERY + "\n"), ("payload_sha256", PAYLOAD + "\n"), ("delivery_id", "B" * 64), ("payload_sha256", "a" * 63)],
)
def test_hash_shape_invalid_corpus_is_rejected_by_schema_and_parser(state, field, value):
    extra = {"reason_code": "validation_failed"} if state == "rejected" else {}
    body = effect_bytes(state, **extra, **{field: value})
    assert list(schema_validator("response").iter_errors(json.loads(body)))
    with pytest.raises(PowerAutomateProtocolError):
        parse_effect(body)
    for operation in ("write", "status"):
        request = {"protocol": PROTOCOL, "operation": operation, "delivery_id": DELIVERY, "payload_sha256": PAYLOAD, field: value}
        if operation == "write":
            request["data"] = {"record_id": "one"}
        assert list(schema_validator(operation).iter_errors(request))


@pytest.mark.parametrize("field", ["snapshot_id", "next_cursor", "receipt_id", "flow_run_id"])
@pytest.mark.parametrize("value", [" ", "\t\n", "\u2003"])
def test_opaque_identifier_invalid_corpus_is_rejected_by_schema_and_parser(field, value):
    is_read = field in ("snapshot_id", "next_cursor")
    body = read_bytes(**{field: value}) if is_read else effect_bytes(**{field: value})
    assert list(schema_validator("response").iter_errors(json.loads(body)))
    with pytest.raises(PowerAutomateProtocolError):
        if is_read:
            parse_read_response(body, page_size=100)
        else:
            parse_effect(body)


@pytest.mark.parametrize(
    "query",
    [
        {"name": "${CREDENTIAL}"},
        {"nested": [{"name": "before ${CREDENTIAL} after"}]},
        {"secret_ref": "locator"},
        {"nested": {"X-Auth-Token": "value"}},
        {"credentials": "value"},
        {"nested": [{"CLIENT_SECRET": "value"}]},
        {"name": {"code": "value"}},
    ],
)
def test_read_request_schema_rejects_same_credential_corpus_as_config(query):
    request = {"protocol": PROTOCOL, "operation": "read", "query": query, "snapshot_id": None, "cursor": None, "page_size": 100}
    assert list(schema_validator("read").iter_errors(request))
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(source_options(query=query))


@pytest.mark.parametrize(
    "query",
    [
        {"dataset": "records"},
        {"author": "public", "nested": [None, 1, True, "ordinary"]},
        {"xapikey": "value"},
        {"my-authorization": "value"},
    ],
)
def test_read_request_schema_credential_classifier_matches_configuration(query):
    request = {"protocol": PROTOCOL, "operation": "read", "query": query, "snapshot_id": None, "cursor": None, "page_size": 100}
    schema_rejects = bool(list(schema_validator("read").iter_errors(request)))
    try:
        PowerAutomateSourceConfig.model_validate(source_options(query=query))
    except ValidationError:
        assert schema_rejects
    else:
        assert not schema_rejects


@pytest.mark.parametrize(
    "factory,options,marker",
    [
        (PowerAutomateSourceConfig, source_options(field_mapping={"secret-value-with-spaces": "x"}), "secret-value-with-spaces"),
        (PowerAutomateSourceConfig, source_options(field_mapping={"x": "secret-value-with-spaces"}), "secret-value-with-spaces"),
        (PowerAutomateSourceConfig, source_options(schema={"mode": "fixed", "fields": ["SecretFieldName: str"]}), "SecretFieldName"),
        (PowerAutomateSourceConfig, source_options(schema={"mode": "flexible", "fields": ["SecretFieldName: str"]}), "SecretFieldName"),
        (PowerAutomateSinkConfig, sink_options(fields=["secret-value-with-spaces"]), "secret-value-with-spaces"),
        (PowerAutomateSourceConfig, source_options(auth={"method": "secret-discriminator"}), "secret-discriminator"),
        (PowerAutomateSourceConfig, source_options(**{"secret-option-name": "value"}), "secret-option-name"),
        (PowerAutomateSourceConfig, source_options(schema={"mode": "secret-schema-mode"}), "secret-schema-mode"),
    ],
)
def test_configuration_diagnostics_redact_boundary_values(factory, options, marker):
    with pytest.raises(ValidationError) as caught:
        factory.model_validate(options)
    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("value", ["false", "true", 0, None])
def test_snapshot_for_resume_strict_invalid_corpus(value):
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(source_options(snapshot_for_resume=value))


def test_valid_long_trigger_url_is_preserved():
    url = ORIGIN + "/" + "long-segment%2F" * 50 + "?sig=a%252Fb"
    assert len(url.encode("utf-8")) > 255
    assert (
        PowerAutomateSourceConfig.model_validate(
            source_options(auth={"method": "sas_url", "trigger_url_secret": url})
        ).auth.trigger_url_secret.get_secret_value()
        == url
    )


@pytest.mark.parametrize("state", ["applied", "not_applied", "unknown", "rejected"])
@pytest.mark.parametrize("mutation", ["missing_delivery", "missing_payload", "extra", "wrong_delivery", "wrong_payload"])
def test_every_effect_state_checks_envelope_and_request_identity(state, mutation):
    extra = {"reason_code": "policy_denied"} if state == "rejected" else {}
    value = json.loads(effect_bytes(state, **extra))
    if mutation == "missing_delivery":
        del value["delivery_id"]
    elif mutation == "missing_payload":
        del value["payload_sha256"]
    elif mutation == "extra":
        value["extra"] = "value"
    elif mutation == "wrong_delivery":
        value["delivery_id"] = "c" * 64
    else:
        value["payload_sha256"] = "c" * 64
    with pytest.raises(PowerAutomateProtocolError):
        parse_effect(json.dumps(value).encode())


@pytest.mark.parametrize("key", ["\u017fig", "\u212aey", "pa\u017f\u017fword", "paßword", "paẞword", "\u017fecret_ref"])
def test_unicode_sensitive_query_key_schema_matches_runtime(key):
    query = {key: "value"}
    request = {"protocol": PROTOCOL, "operation": "read", "query": query, "snapshot_id": None, "cursor": None, "page_size": 100}
    assert list(schema_validator("read").iter_errors(request))
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(source_options(query=query))


@pytest.mark.parametrize("key", ["\u212atoken", "x-\u017fig", "名字", "résultat", "Secrét", "code\n", "token\u0130"])
def test_benign_unicode_query_keys_remain_admitted_by_schema_and_runtime(key):
    query = {key: "value"}
    request = {"protocol": PROTOCOL, "operation": "read", "query": query, "snapshot_id": None, "cursor": None, "page_size": 100}
    schema_validator("read").validate(request)
    assert PowerAutomateSourceConfig.model_validate(source_options(query=query)).query[key] == "value"


def test_shared_generic_options_keep_archived_auth_separate_from_live_configs():
    from pydantic import BaseModel, ConfigDict

    from elspeth.plugins.infrastructure.power_automate import PowerAutomateSinkOptions, PowerAutomateSourceOptions

    class ArchivedAuth(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)
        method: str
        trigger_url_secret_fingerprint: str

    archived = {"method": "sas_url", "trigger_url_secret_fingerprint": "hmac-sha256:" + "a" * 64}
    options = source_options(auth=archived)
    cfg = PowerAutomateSourceOptions[ArchivedAuth].model_validate(options)
    assert cfg.auth.trigger_url_secret_fingerprint == archived["trigger_url_secret_fingerprint"]
    with pytest.raises(ValidationError):
        PowerAutomateSourceConfig.model_validate(options)
    archived_sink = sink_options(auth=archived, trigger_url=None)
    assert PowerAutomateSinkOptions[ArchivedAuth].model_validate(archived_sink).fields == ("record_id",)
    with pytest.raises(ValidationError):
        PowerAutomateSinkConfig.model_validate(archived_sink)
    assert PowerAutomateSourceConfig._plugin_component_type == "source"
    assert PowerAutomateSinkConfig._plugin_component_type == "sink"
