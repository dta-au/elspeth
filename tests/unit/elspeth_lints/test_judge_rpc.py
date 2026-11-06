"""Hostile controls for the externally supervised judge boundary."""

from __future__ import annotations

import hashlib
import json
import socket
import struct
from pathlib import Path
from unittest.mock import Mock

import pytest

from elspeth_lints.core import judge_rpc, source_snapshot
from elspeth_lints.core.judge import AgentToolScope, JudgeConfigurationError, JudgeContractError, JudgeRequest, JudgeTransportError
from elspeth_lints.core.judge_rpc import (
    MAX_FRAME_BYTES,
    decode_json,
    make_request,
    parse_observation,
    receive_frame,
)


def _request() -> dict[str, object]:
    return make_request(
        JudgeRequest("module.py", "R1", "Widget.lookup", "abc", "External boundary", "value = payload.get('x')"),
        model_id="model",
        max_tokens=100,
        transport="openrouter",
        tool_scope=None,
    )


def _reply(request: dict[str, object]) -> dict[str, object]:
    return {
        "version": 1,
        "request_id": request["request_id"],
        "request_sha256": request["request_sha256"],
        "transport": "openrouter",
        "status": "ok",
        "observation": {
            "raw_text": '{"verdict":"BLOCKED","rationale":"Wrong boundary","confidence":0.9,"should_use_decorator":null}',
            "served_model_id": "served-model",
            "prompt_tokens_total": 9,
            "prompt_tokens_cached": None,
        },
    }


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}', b"[]", b'{"x":"\xff"}'])
def test_json_boundary_rejects_ambiguous_or_nonfinite_json(raw: bytes) -> None:
    with pytest.raises(JudgeContractError):
        decode_json(raw)


def test_json_boundary_has_working_positive_control() -> None:
    assert decode_json(b'{"x":1,"nested":[null,false,0]}') == {"x": 1, "nested": [None, False, 0]}


def test_json_boundary_rejects_depth_and_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(judge_rpc, "MAX_FRAME_BYTES", 512)
    for raw in (b'{"x":' * 20 + b"0" + b"}" * 20, b" " * (judge_rpc.MAX_FRAME_BYTES + 1)):
        with pytest.raises(JudgeContractError):
            decode_json(raw)


def test_readonly_evidence_preserves_large_tracked_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "large-tracked-module.py"
    original = "# ordinary evidence\n" * 80_000
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(source_snapshot, "tracked_relative_paths", lambda _root: (target.name,))

    files, excluded = judge_rpc.capture_evidence(AgentToolScope((tmp_path,), tmp_path, 8))

    assert files == [{"root": 0, "path": target.name, "text": original}]
    assert excluded == []


def test_readonly_evidence_preserves_the_selected_nested_allowlist_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allowlist = tmp_path / "config" / "allowlist"
    allowlist.mkdir(parents=True)
    target = allowlist / "_defaults.yaml"
    target.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setattr(source_snapshot, "tracked_relative_paths", lambda _root: ("config/allowlist/_defaults.yaml",))

    files, excluded = judge_rpc.capture_evidence(AgentToolScope((tmp_path, allowlist), tmp_path, 8))

    assert {item["root"] for item in files} == {0, 1}
    assert {item["path"] for item in files} == {"config/allowlist/_defaults.yaml", "_defaults.yaml"}
    assert excluded == []


@pytest.mark.parametrize("cached", [None, 0, 9])
def test_observation_retains_none_versus_zero_and_served_model(cached: int | None) -> None:
    request = _request()
    reply = _reply(request)
    observation = reply["observation"]
    assert isinstance(observation, dict)
    observation["prompt_tokens_cached"] = cached
    result = parse_observation(reply, request)
    assert result.prompt_tokens_cached == cached
    assert result.served_model_id == "served-model"


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt_tokens_total", True),
        ("prompt_tokens_total", -1),
        ("prompt_tokens_cached", True),
        ("prompt_tokens_cached", 10),
        ("served_model_id", ""),
        ("raw_text", ""),
    ],
)
def test_observation_refuses_invalid_external_fields(field: str, value: object) -> None:
    request = _request()
    reply = _reply(request)
    observation = reply["observation"]
    assert isinstance(observation, dict)
    observation[field] = value
    with pytest.raises(JudgeContractError):
        parse_observation(reply, request)


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "another-call"),
        ("request_sha256", "0" * 64),
        ("transport", "codex_cli"),
        ("version", True),
        ("source_verified", True),
        ("hmac", "not-authority"),
    ],
)
def test_observation_refuses_replay_and_authority_fields(field: str, value: object) -> None:
    request = _request()
    reply = _reply(request)
    reply[field] = value
    with pytest.raises(JudgeContractError):
        parse_observation(reply, request)


def test_blinded_request_has_no_candidate_paths_environment_or_evidence() -> None:
    request = _request()
    encoded = json.dumps(request)
    assert request["evidence"] == []
    assert request["tools"] is None
    assert "HOME" not in encoded
    assert "executable" not in encoded
    assert "HMAC" not in encoded


def test_frame_reader_refuses_oversized_prefix_without_reading_body() -> None:
    channel = Mock(spec=socket.socket)
    channel.recv.return_value = struct.pack("!I", MAX_FRAME_BYTES + 1)
    with pytest.raises(JudgeContractError):
        receive_frame(channel, deadline_seconds=0.1)
    channel.recv.assert_called_once_with(4)


def test_frame_reader_positive_control_consumes_one_complete_frame() -> None:
    channel = Mock(spec=socket.socket)
    raw = b'{"value":1}'
    channel.recv.side_effect = [struct.pack("!I", len(raw)), raw]
    assert receive_frame(channel, deadline_seconds=0.1) == {"value": 1}


def test_frame_reader_refuses_eof_and_timeout() -> None:
    left, right = socket.socketpair()
    try:
        with pytest.raises(JudgeTransportError, match="deadline"):
            receive_frame(left, deadline_seconds=0.01)
        right.close()
        with pytest.raises(JudgeTransportError, match="closed"):
            receive_frame(left, deadline_seconds=0.1)
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("value", [[], {}, True, "unknown-provider"])
def test_correlated_request_rejects_malformed_transport_types(value: object) -> None:
    request = _request()
    request["transport"] = value
    request["request_sha256"] = ""
    request["request_sha256"] = hashlib.sha256(judge_rpc.canonical_json(request)).hexdigest()
    with pytest.raises(JudgeContractError):
        judge_rpc.validate_request(request)


@pytest.mark.parametrize("value", [[], {}, True, "raw-provider-exception"])
def test_correlated_error_rejects_unhashable_or_unknown_category(value: object) -> None:
    request = _request()
    reply = judge_rpc.response_envelope(request, error="transport")
    reply["error"] = value
    with pytest.raises(JudgeContractError):
        parse_observation(reply, request)


def test_readonly_capture_contains_tracked_source_and_tests_but_no_private_or_secret_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/module.py").write_text("value = 1\n")
    (tmp_path / "tests/test_module.py").write_text("assert value == 1\n")
    (tmp_path / ".env").write_text("PRIVATE=synthetic-private-value\n")
    (tmp_path / "untracked-private.txt").write_text("untracked-private-value\n")
    synthetic_key = "AKIA" + "0" * 16
    (tmp_path / "src/credentials.py").write_text(f"key = '{synthetic_key}'\n")
    monkeypatch.setattr(
        source_snapshot, "tracked_relative_paths", lambda _root: (".env", "src/module.py", "src/credentials.py", "tests/test_module.py")
    )
    scope = AgentToolScope((tmp_path,), tmp_path, 8)
    files, excluded = judge_rpc.capture_evidence(scope)
    assert {entry["path"] for entry in files} == {"src/module.py", "tests/test_module.py"}
    assert {entry["reason"] for entry in excluded} == {"private-path", "secret-redaction"}
    encoded = json.dumps((files, excluded))
    assert "synthetic-private-value" not in encoded
    assert "untracked-private" not in encoded
    assert synthetic_key not in encoded
    assert str(tmp_path) not in encoded


@pytest.mark.parametrize("parent_link", [False, True])
def test_readonly_capture_refuses_final_and_parent_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent_link: bool) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.py").write_text("private = 'not evidence'\n")
    root = tmp_path / "root"
    root.mkdir()
    if parent_link:
        (root / "src").symlink_to(outside, target_is_directory=True)
        relative = "src/private.py"
    else:
        (root / "private.py").symlink_to(outside / "private.py")
        relative = "private.py"
    monkeypatch.setattr(source_snapshot, "tracked_relative_paths", lambda _root: (relative,))
    with pytest.raises(JudgeConfigurationError, match="safely"):
        judge_rpc.capture_evidence(AgentToolScope((root,), root, 8))


def test_readonly_capture_detects_bytes_changed_between_observations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "module.py"
    path.write_text("value = 1\n")
    count = 0

    def tracked(_root: Path) -> tuple[str, ...]:
        nonlocal count
        count += 1
        if count == 2:
            path.write_text("value = 2\n")
        return ("module.py",)

    monkeypatch.setattr(source_snapshot, "tracked_relative_paths", tracked)
    with pytest.raises(JudgeConfigurationError, match="changed"):
        judge_rpc.capture_evidence(AgentToolScope((tmp_path,), tmp_path, 8))
