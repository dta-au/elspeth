"""Tests for F2: canonicalize Pydantic ``__cause__`` errors in ARG_ERROR audits.

Disposition: spec §4.2.6 documents that promoted-handler ``ToolArgumentError``
sites raise with ``from pydantic.ValidationError``. The ``__cause__`` chain
carries field-name detail (``loc``/``msg``/``type``) that is auditably
valuable for recovery flows — but the ``input`` / ``url`` / ``ctx`` fields on
each error are leak vectors (``input`` carries the rejected value verbatim).

Option (a) chosen: persist canonicalized cause errors (loc/msg/type tuples
only, no values) into ``result_canonical`` via the ARG_ERROR payload factory.

These tests pin:

1. ``canonicalize_pydantic_cause`` helper produces leak-safe output.
2. The module-level ``_arg_error_payload`` factory threads
   ``validation_errors`` through when the ``__cause__`` is a Pydantic
   ``ValidationError``.
"""

from __future__ import annotations

import json
import tracemalloc
from unittest.mock import patch

from pydantic import BaseModel, ValidationError, field_validator

from elspeth.web.composer.audit import canonicalize_pydantic_cause
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import SetSourceArgumentsModel
from elspeth.web.composer.service import _arg_error_payload


class _IntFieldModel(BaseModel):
    """Two-field model: ``x`` (required ``int``) for ``missing`` / ``int_parsing``."""

    x: int


class _ListIntModel(BaseModel):
    """Single ``list[int]`` field for forcing an int-loc element (list index)."""

    items: list[int]


_PYDANTIC_MESSAGE_CANARY = "PYDANTIC_MESSAGE_CANARY_sk_live_8Ft2_/srv/private.key"
_PYDANTIC_LOC_CANARY = "PYDANTIC_LOC_CANARY_sk_live_3Jm7"


class _HostileValidationModel(BaseModel):
    """Produces excessive depth/count plus attacker-controlled loc/msg strings."""

    items: dict[str, list[list[list[int]]]]
    secret: str

    @field_validator("secret")
    @classmethod
    def _reject_secret(cls, value: str) -> str:
        raise ValueError(f"rejected by validator: {_PYDANTIC_MESSAGE_CANARY}")


def _make_int_parsing_error() -> ValidationError:
    """Force a ``ValidationError`` with ``loc=("x",)`` and ``type="int_parsing"``."""
    try:
        _IntFieldModel.model_validate({"x": "not-an-int"})
    except ValidationError as exc:
        return exc
    raise AssertionError("model_validate should have raised")


def _make_missing_error() -> ValidationError:
    """Force a ``ValidationError`` with ``loc=("x",)`` and ``type="missing"``."""
    try:
        _IntFieldModel.model_validate({})
    except ValidationError as exc:
        return exc
    raise AssertionError("model_validate should have raised")


def _make_list_index_error() -> ValidationError:
    """Force a ``ValidationError`` with a list-index int in ``loc``."""
    try:
        _ListIntModel.model_validate({"items": [1, "bad", 3]})
    except ValidationError as exc:
        return exc
    raise AssertionError("model_validate should have raised")


def _make_hostile_validation_error() -> ValidationError:
    try:
        _HostileValidationModel.model_validate(
            {
                "items": {_PYDANTIC_LOC_CANARY: [[["bad"] * 4]]},
                "secret": "trigger",
            }
        )
    except ValidationError as exc:
        raw = json.dumps(exc.errors(), default=str)
        assert _PYDANTIC_LOC_CANARY in raw
        assert _PYDANTIC_MESSAGE_CANARY in raw
        return exc
    raise AssertionError("model_validate should have raised")


# ---------------------------------------------------------------------------
# Helper unit tests.
# ---------------------------------------------------------------------------


def test_canonicalize_pydantic_cause_returns_none_for_none() -> None:
    """``None`` in → ``None`` out (no chained cause to canonicalize)."""
    assert canonicalize_pydantic_cause(None) is None


def test_canonicalize_pydantic_cause_returns_none_for_non_pydantic() -> None:
    """Non-Pydantic exceptions yield ``None`` — the helper opts out cleanly."""
    assert canonicalize_pydantic_cause(ValueError("plain old error")) is None
    assert canonicalize_pydantic_cause(KeyError("missing")) is None
    assert canonicalize_pydantic_cause(RuntimeError("runtime")) is None


def test_canonicalize_pydantic_cause_strips_input_url_ctx() -> None:
    """``input`` / ``url`` / ``ctx`` MUST NOT appear in the canonicalized output.

    ``input`` is the primary leak vector — it carries the rejected
    (LLM-supplied, Tier-3) value verbatim. ``url`` is Pydantic's
    documentation URL (not load-bearing for audit). ``ctx`` may carry the
    rejected value in its context dict. The helper strips all three.
    """
    exc = _make_int_parsing_error()
    result = canonicalize_pydantic_cause(exc)
    assert result is not None
    assert len(result) == 1
    entry = result[0]
    assert set(entry.keys()) == {"loc", "msg", "type"}
    assert "input" not in entry
    assert "url" not in entry
    assert "ctx" not in entry


def test_canonicalize_pydantic_cause_projects_missing_error() -> None:
    """Missing-field detail survives only as fixed semantic diagnostics."""
    exc = _make_missing_error()
    result = canonicalize_pydantic_cause(exc)
    assert result is not None
    assert len(result) == 1
    entry = result[0]
    assert entry["loc"] == ["field"]
    assert entry["type"] == "missing"
    assert entry["msg"] == "Required value is missing"


def test_canonicalize_pydantic_cause_projects_int_parsing_type() -> None:
    """Pydantic parsing codes map to the fixed invalid-type diagnostic."""
    exc = _make_int_parsing_error()
    result = canonicalize_pydantic_cause(exc)
    assert result is not None
    assert result[0]["type"] == "invalid_type"
    assert result[0]["loc"] == ["field"]


def test_canonicalize_pydantic_cause_projects_list_index_loc() -> None:
    """List indices survive only as the fixed ``index`` location token."""
    exc = _make_list_index_error()
    result = canonicalize_pydantic_cause(exc)
    assert result is not None
    list_index_entries = [e for e in result if "index" in e["loc"]]
    assert list_index_entries, f"expected items-loc entry, got {result}"
    entry = list_index_entries[0]
    assert entry["loc"] == ["field", "index"]


def test_canonicalize_pydantic_cause_is_closed_bounded_and_canary_free() -> None:
    """Raw loc/msg/type data is projected to fixed bounded diagnostics."""
    result = canonicalize_pydantic_cause(_make_hostile_validation_error())
    assert result is not None

    serialized = json.dumps(result, sort_keys=True)
    assert _PYDANTIC_LOC_CANARY not in serialized
    assert _PYDANTIC_MESSAGE_CANARY not in serialized
    assert len(result) <= 8
    assert all(len(entry["loc"]) <= 4 for entry in result)
    assert all(set(entry) == {"loc", "msg", "type"} for entry in result)
    assert all(piece in {"field", "index", "item"} for entry in result for piece in entry["loc"])
    assert all(
        entry["type"] in {"invalid", "invalid_choice", "invalid_type", "invalid_value", "missing", "out_of_bounds", "unexpected"}
        for entry in result
    )
    assert all(len(entry["msg"]) <= 64 for entry in result)


def test_real_schema_fields_remain_distinguishable_in_payload() -> None:
    """Closed model field names survive while rejected values remain absent."""
    try:
        SetSourceArgumentsModel.model_validate(
            {
                "plugin": 101,
                "on_success": 202,
                "options": {},
                "on_validation_failure": "quarantine",
            }
        )
    except ValidationError as cause:
        arg_err = ToolArgumentError(
            argument="set_source arguments",
            expected="object conforming to SetSourceArgumentsModel",
            actual_type=type(cause).__name__,
        )
        arg_err.__cause__ = cause
    else:
        raise AssertionError("model_validate should have raised")

    payload = _arg_error_payload(arg_err, "set_source")
    errors = payload["validation_errors"]
    assert isinstance(errors, list)
    assert {tuple(entry["loc"]) for entry in errors} == {("plugin",), ("on_success",)}
    serialized = json.dumps(payload, sort_keys=True)
    assert "101" not in serialized
    assert "202" not in serialized


def test_oversized_validation_error_set_uses_fixed_truncated_diagnostic() -> None:
    """Error-count gating avoids materializing an attacker-sized Python list."""

    class _ManyErrorsModel(BaseModel):
        values: list[int]

    cause_error: ValidationError | None = None
    try:
        _ManyErrorsModel.model_validate({"values": ["bad"] * 20_000})
    except ValidationError as cause:
        cause_error = cause
        assert cause_error.error_count() == 20_000
    else:
        raise AssertionError("model_validate should have raised")
    assert cause_error is not None

    tracemalloc.start()
    try:
        with patch.object(ValidationError, "errors", side_effect=AssertionError("errors() must not materialize")):
            result = canonicalize_pydantic_cause(cause_error)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result == [
        {
            "loc": [],
            "msg": "Validation produced more than 8 errors",
            "type": "truncated",
        }
    ]
    assert peak < 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# Factory integration test.
# ---------------------------------------------------------------------------


def test_arg_error_payload_factory_threads_validation_errors() -> None:
    """``_arg_error_payload`` includes ``validation_errors`` when ``__cause__`` is Pydantic."""
    cause = _make_int_parsing_error()
    arg_err = ToolArgumentError(argument="x", expected="an integer", actual_type="str")
    arg_err.__cause__ = cause
    payload = _arg_error_payload(arg_err, "set_metadata")
    assert "error" in payload
    assert "Tool 'set_metadata' failed" in payload["error"]
    assert "validation_errors" in payload
    assert isinstance(payload["validation_errors"], list)
    assert len(payload["validation_errors"]) == 1
    assert payload["validation_errors"][0]["type"] == "invalid_type"
    assert payload["validation_errors"][0]["loc"] == ["field"]


def test_arg_error_payload_factory_omits_validation_errors_for_non_pydantic_cause() -> None:
    """A non-Pydantic ``__cause__`` (or no cause) yields no ``validation_errors`` key.

    Recording ``validation_errors: []`` (or ``validation_errors: None``)
    has no audit value — the absence of the key is the signal.
    """
    arg_err = ToolArgumentError(argument="x", expected="an integer", actual_type="str")
    arg_err.__cause__ = ValueError("not pydantic")
    payload = _arg_error_payload(arg_err, "set_metadata")
    assert "validation_errors" not in payload

    arg_err_no_cause = ToolArgumentError(argument="y", expected="a string", actual_type="int")
    payload_no_cause = _arg_error_payload(arg_err_no_cause, "set_metadata")
    assert "validation_errors" not in payload_no_cause


def test_arg_error_payload_factory_strips_leak_vectors_end_to_end() -> None:
    """End-to-end leak check: rejected value is NOT in the factory output."""
    cause = _make_int_parsing_error()
    # Confirm the rejected value lives on the cause's errors() output.
    raw_errors = cause.errors()
    assert raw_errors[0]["input"] == "not-an-int"
    arg_err = ToolArgumentError(argument="x", expected="an integer", actual_type="str")
    arg_err.__cause__ = cause
    payload = _arg_error_payload(arg_err, "set_metadata")
    # Walk the payload exhaustively for the rejected value.
    import json

    serialized = json.dumps(payload, default=str)
    assert "not-an-int" not in serialized, f"rejected value leaked into payload: {serialized}"


def test_arg_error_payload_factory_strips_hostile_pydantic_loc_and_message() -> None:
    """The HTTP/prompt/audit-relevant JSON payload contains only closed diagnostics."""
    arg_err = ToolArgumentError(argument="content", expected="a string", actual_type="str")
    arg_err.__cause__ = _make_hostile_validation_error()

    serialized = json.dumps(_arg_error_payload(arg_err, "set_metadata"), sort_keys=True)
    assert _PYDANTIC_LOC_CANARY not in serialized
    assert _PYDANTIC_MESSAGE_CANARY not in serialized
