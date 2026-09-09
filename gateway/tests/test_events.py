import json
import logging

import pytest
from elspeth_llm_gateway.core.events import SAFE_FIELDS, canonical_hash, log_event


def test_unknown_field_raises_value_error():
    logger = logging.getLogger("test.events")
    with pytest.raises(ValueError):
        log_event(logger, "completion", prompt_text="leaked prompt content")


def test_unknown_field_raises_before_any_logging(caplog):
    logger = logging.getLogger("test.events.unsafe")
    caplog.set_level(logging.INFO, logger=logger.name)
    with pytest.raises(ValueError):
        log_event(logger, "completion", request_id="abc", not_a_safe_field="x")
    assert caplog.records == []


def test_every_safe_field_is_individually_accepted(caplog):
    logger = logging.getLogger("test.events.allowlist")
    caplog.set_level(logging.INFO, logger=logger.name)
    for field in SAFE_FIELDS - {"event"}:
        log_event(logger, "completion", **{field: "value"})
    assert len(caplog.records) == len(SAFE_FIELDS) - 1


def test_log_event_logs_expected_fields_as_json(caplog):
    logger = logging.getLogger("test.events.shape")
    caplog.set_level(logging.INFO, logger=logger.name)
    log_event(logger, "completion", request_id="req-1", status="success", latency_ms=12)
    assert len(caplog.records) == 1
    payload = json.loads(caplog.records[0].getMessage())
    assert payload == {"event": "completion", "request_id": "req-1", "status": "success", "latency_ms": 12}


def test_canonical_hash_is_order_independent():
    assert canonical_hash({"b": 1, "a": 2}) == canonical_hash({"a": 2, "b": 1})


def test_canonical_hash_differs_for_different_content():
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_canonical_hash_is_32_hex_chars():
    digest = canonical_hash({"a": 1})
    assert len(digest) == 32
    int(digest, 16)  # raises ValueError if not valid hex
