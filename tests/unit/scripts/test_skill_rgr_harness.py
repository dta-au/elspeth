"""Tests for the skill RGR harness."""

from __future__ import annotations

from collections import UserDict
from pathlib import Path
from typing import Any, get_type_hints

import litellm
import pytest
from litellm.types.utils import Message
from scripts.skill_rgr import harness

from elspeth.web.composer import tool_result_envelope as env
from elspeth.web.composer.tools._common import (
    _FULL_STATE_COMPONENT_ALIASES,
    _FullPipelineStateInspectionPayload,
    _FullPipelineStateMetadataPayload,
    _FullPipelineStatePayload,
)


class _FakeMessage:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return self._payload


class _FakeChoice:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.message = _FakeMessage(payload)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.choices = [_FakeChoice(payload)]


class _MessageChoice:
    def __init__(self, message: object) -> None:
        self.message = message


class _MessageResponse:
    def __init__(self, message: object) -> None:
        self.choices = [_MessageChoice(message)]


class _DictSubclass(dict[str, Any]):
    pass


def test_run_scenario_rejects_malformed_tool_call_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stub_calls: list[dict[str, Any]] = []

    def stub(args: dict[str, Any]) -> dict[str, str]:
        stub_calls.append(args)
        return {"status": "should-not-run"}

    monkeypatch.setattr(harness, "TRANSCRIPTS_DIR", tmp_path)
    monkeypatch.setattr(
        harness,
        "get_tool_definitions",
        lambda: [
            {
                "name": "set_source",
                "description": "Set a source",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    )
    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **_kwargs: _FakeResponse(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_bad_json",
                        "type": "function",
                        "function": {"name": "set_source", "arguments": "{not-json"},
                    }
                ],
            }
        ),
    )
    scenario = harness.Scenario(
        name="malformed_tool_call_json",
        user_prompt="please set a source",
        stubs={"set_source": stub},
        max_turns=1,
        green_predicates=[lambda transcript: harness.called_tool(transcript, "set_source")],
    )

    transcript = harness.run_scenario(
        scenario,
        skill_text="system",
        model="test-model",
        label="red",
    )

    assert stub_calls == []
    assert harness.called_tool(transcript, "set_source") is False
    assert harness.evaluate(transcript, scenario, phase="green") == {"p0": False}
    tool_errors = [entry for entry in transcript if entry.get("role") == "tool_argument_error"]
    assert len(tool_errors) == 1
    assert tool_errors[0]["name"] == "set_source"
    assert tool_errors[0]["raw_args"] == "{not-json"


def test_run_scenario_pins_sampling_controls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_kwargs: dict[str, Any] = {}

    monkeypatch.setattr(harness, "TRANSCRIPTS_DIR", tmp_path)
    monkeypatch.setattr(harness, "get_tool_definitions", lambda: [])

    def fake_completion(**kwargs: Any) -> _FakeResponse:
        captured_kwargs.update(kwargs)
        return _FakeResponse({"role": "assistant", "content": "done", "tool_calls": []})

    monkeypatch.setattr(litellm, "completion", fake_completion)
    scenario = harness.Scenario(
        name="sampling_controls",
        user_prompt="compose a pipeline",
        max_turns=1,
    )

    harness.run_scenario(
        scenario,
        skill_text="system",
        model="test-model",
        label="red",
    )

    assert captured_kwargs["temperature"] == 0
    assert captured_kwargs["seed"] == 0
    assert captured_kwargs["drop_params"] is True


def test_run_scenario_accepts_a_real_litellm_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(harness, "TRANSCRIPTS_DIR", tmp_path)
    monkeypatch.setattr(harness, "get_tool_definitions", lambda: [])
    message = Message(content="done", role="assistant")
    monkeypatch.setattr(litellm, "completion", lambda **_kwargs: _MessageResponse(message))
    scenario = harness.Scenario(name="real-message", user_prompt="compose", max_turns=1)

    transcript = harness.run_scenario(
        scenario,
        skill_text="system",
        model="test-model",
        label="red",
    )

    assert transcript[-1]["role"] == "assistant"
    assert transcript[-1]["content"] == "done"


@pytest.mark.parametrize(
    "malformed",
    [
        [],
        _DictSubclass(role="assistant", content="done"),
        {1: "non-string-key", "role": "assistant", "content": "done"},
    ],
)
def test_run_scenario_rejects_a_non_exact_dict_message_dump(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    malformed: object,
) -> None:
    class MalformedMessage:
        def model_dump(self) -> object:
            return malformed

    monkeypatch.setattr(harness, "TRANSCRIPTS_DIR", tmp_path)
    monkeypatch.setattr(harness, "get_tool_definitions", lambda: [])
    monkeypatch.setattr(litellm, "completion", lambda **_kwargs: _MessageResponse(MalformedMessage()))
    scenario = harness.Scenario(name="malformed-message", user_prompt="compose", max_turns=1)

    with pytest.raises(TypeError, match="model_dump must return an exact dict"):
        harness.run_scenario(
            scenario,
            skill_text="system",
            model="test-model",
            label="red",
        )


@pytest.mark.parametrize(
    "malformed",
    [
        {"role": "user", "content": "not an assistant"},
        {"role": "assistant", "content": "done", "turn": 999},
    ],
)
def test_run_scenario_rejects_message_fields_that_override_owned_transcript_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    malformed: dict[str, Any],
) -> None:
    class MalformedMessage:
        def model_dump(self) -> dict[str, Any]:
            return malformed

    monkeypatch.setattr(harness, "TRANSCRIPTS_DIR", tmp_path)
    monkeypatch.setattr(harness, "get_tool_definitions", lambda: [])
    monkeypatch.setattr(litellm, "completion", lambda **_kwargs: _MessageResponse(MalformedMessage()))
    scenario = harness.Scenario(name="override-message", user_prompt="compose", max_turns=1)

    with pytest.raises(TypeError, match="assistant role without a reserved turn"):
        harness.run_scenario(
            scenario,
            skill_text="system",
            model="test-model",
            label="red",
        )


def test_run_scenario_does_not_admit_a_mapping_without_model_dump(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    message = UserDict({"role": "assistant", "content": "done", "tool_calls": []})
    monkeypatch.setattr(harness, "TRANSCRIPTS_DIR", tmp_path)
    monkeypatch.setattr(harness, "get_tool_definitions", lambda: [])
    monkeypatch.setattr(litellm, "completion", lambda **_kwargs: _MessageResponse(message))
    scenario = harness.Scenario(name="mapping-message", user_prompt="compose", max_turns=1)

    with pytest.raises(AttributeError, match="model_dump"):
        harness.run_scenario(
            scenario,
            skill_text="system",
            model="test-model",
            label="red",
        )


def test_state_read_stub_carries_the_tool_payload_shape() -> None:
    """``get_pipeline_state_result`` is derived from the tool's own payload types.

    The harness loads the LIVE ``pipeline_composer`` skill, so a stub whose
    wire the tool never produces scores a scenario against a fiction: the
    pre-2026-09 stub published a singular ``source`` with an ``outputs``
    MAPPING and no envelope, where the tool ships a ``sources`` mapping,
    ``outputs``/``edges`` lists, ``metadata`` and ``inspection`` under ``data``
    (elspeth-e405ad7cd2 LLM-R3-6; the same defect ``c27587e0f`` fixed for the
    three ``preview_pipeline`` stubs). Deriving the expectation from
    ``_FullPipelineStatePayload`` rather than restating it means a key added to
    the tool reds this test instead of silently ageing the stub.
    """
    result = harness.get_pipeline_state_result()
    assert set(result) >= set(env.TOOL_RESULT_REQUIRED_KEYS)
    data = result["data"]
    assert set(data) == set(get_type_hints(_FullPipelineStatePayload))
    assert set(data["metadata"]) == set(get_type_hints(_FullPipelineStateMetadataPayload))
    assert set(data["inspection"]) == set(get_type_hints(_FullPipelineStateInspectionPayload))
    assert data["inspection"]["accepted_full_state_aliases"] == list(_FULL_STATE_COMPONENT_ALIASES)


def test_default_state_stub_answers_the_source_slice_with_the_slice_shape() -> None:
    """``component="source"`` ships a ``sources``-only ``data``, never the full document.

    ``sessions.py::_execute_get_pipeline_state`` answers that component with
    ``{"sources": ...}`` alone; the full-state arm is the no-component / alias
    arm. A stub that answered both with the full document would teach a reader
    that ``metadata`` and ``inspection`` ride every state read.
    """
    stub = harness._default_stub("get_pipeline_state")
    assert set(stub({"component": "source"})["data"]) == {"sources"}
    assert set(stub({})["data"]) == set(get_type_hints(_FullPipelineStatePayload))
    assert stub({"component": "full"})["data"]["inspection"]["requested_component"] == "full"
