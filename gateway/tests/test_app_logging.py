"""``build()`` makes the gateway's metadata-only events visible on stderr.

Uvicorn configures only its own loggers, so the gateway's INFO events were
dropped unless something attached a handler. ``build()`` attaches exactly
one, and only when the operator has not configured the logger themselves.
"""

import json
import logging

import pytest
from elspeth_llm_gateway.core.app import build
from elspeth_llm_gateway.core.events import log_event

GATEWAY_LOGGER = "elspeth_llm_gateway"

ENV = {
    "ELSPETH_LLM_GATEWAY_INBOUND_BEARER": "b" * 40,
    "ELSPETH_LLM_GATEWAY_ADAPTER": "reference_v1_invoke",
    "ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN": "https://upstream.example.com",
    "ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL": "https://auth.example.com/token",
    "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_ID": "client-id-value",
    "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET": "c" * 40,
    "ELSPETH_LLM_GATEWAY_OAUTH_AUTH_METHOD": "client_secret_basic",
    "ELSPETH_LLM_GATEWAY_MAX_MESSAGES": "50",
    "ELSPETH_LLM_GATEWAY_MAX_TOOLS": "10",
    "ELSPETH_LLM_GATEWAY_MAX_STRING_CHARS": "10000",
    "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_BYTES": "65536",
    "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_DEPTH": "10",
    "ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS": '{"gpt-4o": {"target": "backend-a"}}',
}


@pytest.fixture
def unconfigured_logger() -> logging.Logger:
    gateway_logger = logging.getLogger(GATEWAY_LOGGER)
    gateway_logger.handlers[:] = []
    gateway_logger.setLevel(logging.NOTSET)
    gateway_logger.propagate = True
    return gateway_logger


def test_build_twice_attaches_exactly_one_handler(monkeypatch, unconfigured_logger):
    monkeypatch.setattr("os.environ", dict(ENV))

    build()
    build()

    assert len(unconfigured_logger.handlers) == 1
    assert type(unconfigured_logger.handlers[0]) is logging.StreamHandler
    assert unconfigured_logger.level == logging.INFO
    assert unconfigured_logger.propagate is False


def test_an_event_reaches_stderr_once_after_build(monkeypatch, capsys, unconfigured_logger):
    monkeypatch.setattr("os.environ", dict(ENV))
    build()

    log_event(unconfigured_logger, "completion", request_id="req-visible", status="success")

    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert [json.loads(line) for line in lines] == [{"event": "completion", "request_id": "req-visible", "status": "success"}]
    assert captured.out == ""


def test_an_operator_configured_logger_keeps_its_own_routing(monkeypatch, capsys, unconfigured_logger):
    operator_handler = logging.NullHandler()
    unconfigured_logger.addHandler(operator_handler)
    unconfigured_logger.setLevel(logging.WARNING)
    monkeypatch.setattr("os.environ", dict(ENV))

    build()
    log_event(unconfigured_logger, "completion", request_id="req-operator", status="success")

    assert unconfigured_logger.handlers == [operator_handler]
    assert unconfigured_logger.level == logging.WARNING
    assert unconfigured_logger.propagate is True
    assert capsys.readouterr().err == ""
