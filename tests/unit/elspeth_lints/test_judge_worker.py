"""Transport dispatch and worker authority stay on opposite sides of RPC."""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from elspeth_lints.core import judge, judge_rpc, judge_supervisor, judge_worker


@pytest.mark.parametrize("transport", [judge.TRANSPORT_OPENROUTER, judge.TRANSPORT_AGENT, judge.TRANSPORT_CODEX_CLI])
def test_every_production_transport_uses_worker_client(monkeypatch: pytest.MonkeyPatch, transport: str) -> None:
    observed = judge._TransportResult(
        json.dumps({"verdict": "ACCEPTED", "rationale": "Verified external boundary", "confidence": 0.9, "should_use_decorator": None}),
        "served",
        10,
        None,
    )
    worker = Mock(return_value=observed)
    forbidden = Mock(side_effect=AssertionError("provider executed in signer"))
    monkeypatch.setattr(judge_rpc, "call_worker", worker)
    monkeypatch.setitem(judge._TRANSPORTS, transport, forbidden)
    request = judge.JudgeRequest("module.py", "R1", "Widget.lookup", "abc", "Boundary", "code")
    response = judge.call_judge(request, transport=transport)
    worker.assert_called_once()
    forbidden.assert_not_called()
    assert response.model_id == "served"
    assert response.judge_transport == transport
    assert response.policy_hash == judge.JUDGE_POLICY_HASH


@pytest.mark.parametrize("transport", [judge.TRANSPORT_OPENROUTER, judge.TRANSPORT_AGENT, judge.TRANSPORT_CODEX_CLI])
def test_missing_supervisor_never_falls_back(monkeypatch: pytest.MonkeyPatch, transport: str) -> None:
    worker = Mock(side_effect=judge.JudgeConfigurationError("protected worker unavailable"))
    forbidden = Mock(side_effect=AssertionError("provider executed in signer"))
    monkeypatch.setattr(judge_rpc, "call_worker", worker)
    monkeypatch.setitem(judge._TRANSPORTS, transport, forbidden)
    request = judge.JudgeRequest("module.py", "R1", "Widget.lookup", "abc", "Boundary", "code")
    with pytest.raises(judge.JudgeConfigurationError, match="unavailable"):
        judge.call_judge(request, transport=transport)
    forbidden.assert_not_called()


def test_explicit_fake_transport_retains_existing_test_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = Mock(side_effect=AssertionError("production RPC reached by explicit test seam"))
    monkeypatch.setattr(judge_rpc, "call_worker", forbidden)
    fake = Mock(
        return_value=judge._TransportResult(
            '{"verdict":"BLOCKED","rationale":"Wrong","confidence":0.1,"should_use_decorator":null}', "fake", 0, 0
        )
    )
    request = judge.JudgeRequest("module.py", "R1", "Widget.lookup", "abc", "Boundary", "code")
    result = judge.call_judge(request, transport_impl=fake)
    fake.assert_called_once_with(request, judge.DEFAULT_JUDGE_MODEL, judge.DEFAULT_JUDGE_MAX_TOKENS)
    forbidden.assert_not_called()
    assert result.prompt_tokens_cached == 0


@pytest.mark.parametrize(
    "raw",
    [
        '{"verdict":"ACCEPTED","verdict":"BLOCKED","rationale":"x","confidence":0.5,"should_use_decorator":null}',
        '{"verdict":"ACCEPTED","rationale":"x","confidence":NaN,"should_use_decorator":null}',
    ],
)
def test_owned_verdict_parser_rejects_ambiguous_json(raw: str) -> None:
    request = judge.JudgeRequest("module.py", "R1", "Widget.lookup", "abc", "Boundary", "code")
    fake = Mock(return_value=judge._TransportResult(raw, "fake", 0, None))
    with pytest.raises(judge.JudgeContractError):
        judge.call_judge(request, transport_impl=fake)


def _rpc_request() -> dict[str, object]:
    return judge_rpc.make_request(
        judge.JudgeRequest("module.py", "R1", "Widget.lookup", "abc", "Boundary", "code"),
        model_id="model",
        max_tokens=100,
        transport=judge.TRANSPORT_OPENROUTER,
        tool_scope=None,
    )


def _profile() -> judge_supervisor.WorkerProfile:
    return judge_supervisor.WorkerProfile(
        40001, 40001, Path("/opt/elspeth/worker-image"), "a" * 64, Path("/var/lib/elspeth/providers/openrouter"), "api"
    )


def test_blinded_service_command_contains_no_candidate_mount_or_caller_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _rpc_request()
    monkeypatch.setenv("ARBITRARY_REKEY_VALUE", "synthetic-operator-private-value")
    command = judge_supervisor.service_command(request, _profile(), Path("/run/elspeth-judge/evidence/one"))
    serialized = " ".join(command)
    assert "/evidence" not in serialized.replace("/run/elspeth-judge/evidence/one/profile.json", "profile")
    assert "synthetic-operator-private-value" not in serialized
    assert "RootDirectory=/opt/elspeth/worker-image" in command
    assert "KillMode=control-group" in command
    assert "NoNewPrivileges=yes" in command
    assert "ProtectProc=invisible" in command
    assert "User=40001" in command
    assert "RuntimeMaxSec=300" in command


def test_worker_provider_dispatch_requires_owned_execution_context() -> None:
    with pytest.raises(judge.JudgeConfigurationError, match="independently supervised"):
        judge_worker.dispatch_request(_rpc_request())


@pytest.mark.parametrize("implementation", [judge._call_openrouter, judge._call_codex_cli])
def test_legacy_provider_implementations_refuse_before_startup(implementation: object) -> None:
    assert callable(implementation)
    with pytest.raises(judge.JudgeConfigurationError, match="independently supervised"):
        implementation(judge.JudgeRequest("x.py", "R1", "x", "abc", "Reason", "code"), "model", 100)


def test_sdk_hmac_environment_refusal_remains_before_worker_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", "synthetic")
    with pytest.raises(judge.JudgeConfigurationError, match="HMAC_KEY is present"):
        judge._call_agent_sdk(judge.JudgeRequest("x.py", "R1", "x", "abc", "Reason", "code"), "model", 100)


@pytest.mark.parametrize("failure", [judge.JudgeTransportError("secret raw failure"), judge.JudgeContractError("secret malformed text")])
def test_supervisor_reaps_and_confirms_unit_death_before_returning_error(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    request = _rpc_request()
    events: list[str] = []
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.stdin = Mock()
    process.stdout = Mock()
    process.kill.side_effect = lambda: events.append("kill")
    process.wait.side_effect = lambda **_kwargs: events.append("wait")
    monkeypatch.setattr(judge_supervisor, "materialize_snapshot", lambda *_args: Path("/run/elspeth-judge/evidence/call"))
    monkeypatch.setattr(judge_supervisor, "_check_startup", lambda *_args: None)
    monkeypatch.setattr(judge_supervisor, "validate_artifact_manifest", lambda _path: "a" * 64)
    monkeypatch.setattr(judge_supervisor.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(judge_supervisor, "_exchange_process", Mock(side_effect=failure))
    monkeypatch.setattr(judge_supervisor, "stop_unit", lambda _unit: events.append("unit-stop"))
    monkeypatch.setattr(judge_supervisor, "ensure_unit_dead", lambda _unit, **_kwargs: events.append("unit-dead"))
    monkeypatch.setattr(judge_supervisor, "remove_snapshot", lambda _path: events.append("snapshot-removed"))
    reply = judge_supervisor.run_worker(request, Mock(spec=socket.socket), _profile())
    assert events == ["unit-stop", "kill", "wait", "unit-dead", "snapshot-removed"]
    assert reply["status"] == "error"
    assert "secret" not in json.dumps(reply)


def test_complete_observation_is_refused_if_unit_death_cannot_be_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _rpc_request()
    reply = judge_rpc.response_envelope(
        request, observation={"raw_text": "{}", "served_model_id": "served", "prompt_tokens_total": 0, "prompt_tokens_cached": None}
    )
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = 0
    process.returncode = 0
    process.stdin = Mock()
    process.stdout = Mock()
    monkeypatch.setattr(judge_supervisor, "materialize_snapshot", lambda *_args: Path("/run/elspeth-judge/evidence/call"))
    monkeypatch.setattr(judge_supervisor, "_check_startup", lambda *_args: None)
    monkeypatch.setattr(judge_supervisor, "validate_artifact_manifest", lambda _path: "a" * 64)
    monkeypatch.setattr(judge_supervisor.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(judge_supervisor, "_exchange_process", lambda *_args: judge_rpc.frame_bytes(reply))
    monkeypatch.setattr(judge_supervisor, "stop_unit", lambda _unit: None)
    monkeypatch.setattr(judge_supervisor, "ensure_unit_dead", Mock(side_effect=judge.JudgeTransportError("still alive")))
    monkeypatch.setattr(judge_supervisor, "remove_snapshot", lambda _path: None)
    assert judge_supervisor.run_worker(request, Mock(spec=socket.socket), _profile())["status"] == "error"
    process.wait.assert_called_once_with(timeout=5)


@pytest.mark.parametrize("close_peer", [True, False])
def test_process_exchange_cancels_on_caller_death_or_deadline(monkeypatch: pytest.MonkeyPatch, close_peer: bool) -> None:
    left, right = socket.socketpair()
    # Only stdlib interpreter startup; the fake child has a fresh constant
    # environment and never imports project or optional provider packages.
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin"},
        close_fds=True,
    )
    try:
        if close_peer:
            right.close()
        else:
            monkeypatch.setattr(judge_supervisor, "WORKER_DEADLINE_SECONDS", -4.99)
        with pytest.raises(judge.JudgeTransportError, match=r"closed|deadline"):
            judge_supervisor._exchange_process(process, left, _rpc_request())
    finally:
        process.kill()
        process.wait(timeout=5)
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        left.close()
        right.close()
    assert process.returncode is not None


def test_rpc_peer_authentication_rejects_wrong_uid_before_proc_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = Mock(spec=socket.socket)
    channel.getsockopt.return_value = struct.pack("3i", os.getpid(), 40001, 40001)
    admission = judge_rpc.SupervisorAdmission(
        Path("/opt/supervisor"), "a" * 64, Path("/opt/supervisor/python"), "b" * 64, (os.getuid(),), {}
    )
    with pytest.raises(judge.JudgeConfigurationError, match="peer"):
        judge_rpc.authenticate_supervisor(channel, admission)


def test_nonroot_preview_authenticates_root_peer_when_kernel_hides_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = Mock(spec=socket.socket)
    channel.getsockopt.return_value = struct.pack("3i", os.getpid(), 0, 0)
    admission = judge_rpc.SupervisorAdmission(
        judge_rpc.SUPERVISOR_IMAGE, "a" * 64, judge_rpc.SUPERVISOR_IMAGE / "usr/bin/python3", "b" * 64, (os.getuid(),), {}
    )
    monkeypatch.setattr(judge_rpc.os, "readlink", Mock(side_effect=PermissionError("kernel hides root process")))
    judge_rpc.authenticate_supervisor(channel, admission)


def test_observable_wrong_supervisor_kernel_root_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = Mock(spec=socket.socket)
    channel.getsockopt.return_value = struct.pack("3i", os.getpid(), 0, 0)
    admission = judge_rpc.SupervisorAdmission(
        judge_rpc.SUPERVISOR_IMAGE, "a" * 64, judge_rpc.SUPERVISOR_IMAGE / "usr/bin/python3", "b" * 64, (os.getuid(),), {}
    )
    monkeypatch.setattr(judge_rpc.os, "readlink", lambda path: str(admission.executable) if path.endswith("/exe") else "/wrong-root")
    monkeypatch.setattr(Path, "read_bytes", lambda _path: b"/usr/bin/python3\0-I\0-B\0-m\0elspeth_lints.core.judge_supervisor\0")
    with pytest.raises(judge.JudgeConfigurationError, match="immutable image"):
        judge_rpc.authenticate_supervisor(channel, admission)


@pytest.mark.parametrize("populated,allowed", [("populated 0\n", True), ("populated 1\n", False)])
def test_unit_death_requires_kernel_cgroup_empty_positive_and_negative_controls(
    monkeypatch: pytest.MonkeyPatch, populated: str, allowed: bool
) -> None:
    unit = "elspeth-judge-" + "a" * 32 + ".service"
    status = b"LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlGroup=\n"
    monkeypatch.setattr(judge_supervisor, "_systemctl", lambda *_args: subprocess.CompletedProcess([], 0, status))
    monkeypatch.setattr(Path, "is_file", lambda _path: True)
    monkeypatch.setattr(Path, "is_dir", lambda _path: True)
    monkeypatch.setattr(Path, "read_text", lambda _path, **_kwargs: populated)
    if allowed:
        judge_supervisor.ensure_unit_dead(unit, launcher_reaped=True)
    else:
        with pytest.raises(judge.JudgeTransportError, match="still contains"):
            judge_supervisor.ensure_unit_dead(unit, launcher_reaped=True)


def test_unit_collected_state_requires_reaped_launcher_and_absent_kernel_cgroup(monkeypatch: pytest.MonkeyPatch) -> None:
    status = b"LoadState=not-found\nActiveState=inactive\n"
    monkeypatch.setattr(judge_supervisor, "_systemctl", lambda *_args: subprocess.CompletedProcess([], 1, status))
    monkeypatch.setattr(Path, "is_file", lambda _path: True)
    monkeypatch.setattr(Path, "is_dir", lambda _path: True)
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=FileNotFoundError()))
    unit = "elspeth-judge-" + "a" * 32 + ".service"
    with pytest.raises(judge.JudgeTransportError, match="launcher"):
        judge_supervisor.ensure_unit_dead(unit, launcher_reaped=False)
    judge_supervisor.ensure_unit_dead(unit, launcher_reaped=True)


def test_startup_checks_deadline_and_caller_death_with_open_peer_positive_control() -> None:
    left, right = socket.socketpair()
    try:
        judge_supervisor._check_startup(left, time.monotonic() + 1)
        with pytest.raises(judge.JudgeTransportError, match="startup deadline"):
            judge_supervisor._check_startup(left, time.monotonic() - 1)
        right.close()
        with pytest.raises(judge.JudgeTransportError, match="caller closed"):
            judge_supervisor._check_startup(left, time.monotonic() + 1)
    finally:
        left.close()
        right.close()


def test_installed_operator_runtime_cannot_inject_callable_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth_lints.core import operator_runtime

    monkeypatch.setattr(operator_runtime, "current_operator_runtime", lambda: object())
    injected = Mock(side_effect=AssertionError("in-process transport executed"))
    with pytest.raises(judge.JudgeConfigurationError, match="cannot inject"):
        judge.call_judge(judge.JudgeRequest("x.py", "R1", "x", "abc", "Reason", "code"), transport_impl=injected)
    injected.assert_not_called()
