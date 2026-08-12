"""Unit tests for the value-free composer planner-attempt audit contract."""

from __future__ import annotations

import dataclasses

import pytest

from elspeth.contracts.composer_planner_audit import (
    ComposerPlannerAttempt,
    ComposerPlannerAttemptLedTo,
    ComposerPlannerAttemptOutcome,
    ComposerPlannerAttemptPhase,
    ComposerPlannerAttemptRecorder,
    ComposerPlannerCode,
    ComposerPlannerInformationClass,
)


def _attempt(**overrides: object) -> ComposerPlannerAttempt:
    defaults: dict[str, object] = {
        "ordinal": 1,
        "planner_call_ordinal": 3,
        "phase": ComposerPlannerAttemptPhase.CANDIDATE,
        "outcome": ComposerPlannerAttemptOutcome.CANDIDATE_REJECTED,
        "planner_code": ComposerPlannerCode.VALIDATION_FAILED,
        "selected_tools": ("set_pipeline",),
        "requested_information": (ComposerPlannerInformationClass.PLUGIN_SCHEMA,),
        "new_information": (),
        "rejection_codes": ("no_source_configured",),
        "candidate_shape_hash": "a" * 64,
        "repeated_fingerprint": False,
        "led_to": ComposerPlannerAttemptLedTo.REPAIR,
    }
    defaults.update(overrides)
    return ComposerPlannerAttempt(**defaults)  # type: ignore[arg-type]


def test_attempt_serializes_only_closed_value_free_fields() -> None:
    payload = _attempt().to_dict()

    assert payload == {
        "ordinal": 1,
        "planner_call_ordinal": 3,
        "phase": "candidate",
        "outcome": "candidate_rejected",
        "planner_code": "VALIDATION_FAILED",
        "selected_tools": ["set_pipeline"],
        "requested_information": ["plugin.schema"],
        "new_information": [],
        "rejection_codes": ["no_source_configured"],
        "candidate_shape_hash": "a" * 64,
        "repeated_fingerprint": False,
        "led_to": "repair",
    }


def test_attempt_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _attempt().ordinal = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value", "match"),
    [
        ("ordinal", True, "ordinal must be a positive exact integer"),
        ("planner_call_ordinal", 0, "planner_call_ordinal must be a positive exact integer"),
        ("phase", "candidate", "phase must be ComposerPlannerAttemptPhase"),
        ("outcome", "accepted", "outcome must be ComposerPlannerAttemptOutcome"),
        ("planner_code", "VALIDATION_FAILED", "planner_code must be ComposerPlannerCode or None"),
        ("selected_tools", ["set_pipeline"], "selected_tools must be an exact tuple"),
        ("requested_information", [ComposerPlannerInformationClass.PLUGIN_SCHEMA], "requested_information must be an exact tuple"),
        ("new_information", [ComposerPlannerInformationClass.PLUGIN_SCHEMA], "new_information must be an exact tuple"),
        ("rejection_codes", ["no_source_configured"], "rejection_codes must be an exact tuple"),
        ("repeated_fingerprint", 0, "repeated_fingerprint must be an exact bool"),
        ("led_to", "repair", "led_to must be ComposerPlannerAttemptLedTo"),
    ],
)
def test_attempt_rejects_non_exact_field_types(field_name: str, value: object, match: str) -> None:
    with pytest.raises((TypeError, ValueError), match=match):
        _attempt(**{field_name: value})


@pytest.mark.parametrize(
    ("field_name", "value", "match"),
    [
        ("selected_tools", ("set-pipeline",), "selected_tools entries"),
        ("selected_tools", ("\N{CYRILLIC SMALL LETTER DZE}et_pipeline",), "selected_tools entries"),
        ("rejection_codes", ("contains private prose",), "rejection_codes entries"),
        ("rejection_codes", ("no_source_configured", "no_source_configured"), "rejection_codes entries must be unique"),
        ("candidate_shape_hash", "A" * 64, "candidate_shape_hash"),
    ],
)
def test_attempt_rejects_open_or_malformed_evidence(field_name: str, value: object, match: str) -> None:
    with pytest.raises((TypeError, ValueError), match=match):
        _attempt(**{field_name: value})


def test_attempt_preserves_repeated_tool_and_information_order() -> None:
    attempt = _attempt(
        selected_tools=("get_plugin_schema", "get_plugin_schema"),
        requested_information=(
            ComposerPlannerInformationClass.PLUGIN_SCHEMA,
            ComposerPlannerInformationClass.PLUGIN_SCHEMA,
        ),
        new_information=(
            ComposerPlannerInformationClass.PLUGIN_SCHEMA,
            ComposerPlannerInformationClass.PLUGIN_SCHEMA,
        ),
    )

    assert attempt.to_dict()["selected_tools"] == ["get_plugin_schema", "get_plugin_schema"]
    assert attempt.to_dict()["requested_information"] == ["plugin.schema", "plugin.schema"]
    assert attempt.to_dict()["new_information"] == ["plugin.schema", "plugin.schema"]


def test_attempt_accepts_absent_optional_fields_for_non_candidate_response() -> None:
    attempt = _attempt(
        phase=ComposerPlannerAttemptPhase.RESPONSE,
        outcome=ComposerPlannerAttemptOutcome.MALFORMED_RESPONSE,
        planner_code=ComposerPlannerCode.MALFORMED_RESPONSE,
        selected_tools=(),
        requested_information=(),
        candidate_shape_hash=None,
        rejection_codes=(),
        led_to=ComposerPlannerAttemptLedTo.TERMINAL,
    )

    assert attempt.to_dict()["candidate_shape_hash"] is None


def test_recorder_protocol_is_structurally_usable() -> None:
    class _Recorder:
        def record_planner_attempt(self, attempt: ComposerPlannerAttempt) -> None:
            self.attempt = attempt

    recorder: ComposerPlannerAttemptRecorder = _Recorder()
    recorder.record_planner_attempt(_attempt())
