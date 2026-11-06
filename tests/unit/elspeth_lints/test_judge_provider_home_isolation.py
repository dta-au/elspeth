"""Judge workers never give a model tool or shell authority over provider credentials.

The installed judge worker mounts its independently provisioned provider home
(credentials and account state) at ``/provider`` and points ``HOME``,
``CODEX_HOME`` and ``CLAUDE_CONFIG_DIR`` at it. A model-steered tool that can
read that home can return credentials in its rationale. These tests use
synthetic sentinels only.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from elspeth_lints.core import judge, judge_rpc, judge_worker

# Built at runtime so no credential-shaped literal exists in source.
_SENTINEL = "sk-" + "ant-" + "SYNTHETICSENTINEL" + "0" * 40


def _request() -> judge.JudgeRequest:
    return judge.JudgeRequest("module.py", "R1", "Widget.lookup", "abc", "Boundary", "code")


def _scope(root: Path) -> judge.AgentToolScope:
    return judge.AgentToolScope(allowed_roots=(root,), cwd=root, max_turns=4)


@pytest.mark.parametrize("readonly", [False, True])
def test_codex_cli_transport_refuses_before_any_worker_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, readonly: bool) -> None:
    worker = create_autospec(judge_rpc.call_worker, spec_set=True, side_effect=AssertionError("codex request reached the worker"))
    monkeypatch.setattr(judge_rpc, "call_worker", worker)
    scope = _scope(tmp_path) if readonly else None
    with pytest.raises(judge.JudgeConfigurationError, match="codex-cli judge transport is disabled"):
        judge.call_judge(_request(), transport=judge.TRANSPORT_CODEX_CLI, tool_scope=scope)
    worker.assert_not_called()


def test_worker_refuses_codex_cli_before_reading_profile_or_provider_home(monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = create_autospec(judge_worker.os.open, spec_set=True, side_effect=AssertionError("provider home opened"))
    monkeypatch.setattr(judge_worker.os, "open", forbidden)
    request = {"transport": judge.TRANSPORT_CODEX_CLI, "request_id": "0" * 32, "request_sha256": "0" * 64, "evidence_sha256": "0" * 64}
    with pytest.raises(judge.JudgeConfigurationError, match="codex-cli judge transport is disabled"):
        judge_worker._admit_profile(request)
    forbidden.assert_not_called()


@pytest.mark.parametrize("readonly", [False, True])
def test_rationale_is_scrubbed_in_every_tool_mode(tmp_path: Path, readonly: bool) -> None:
    leaked = json.dumps(
        {"verdict": "BLOCKED", "rationale": f"provider-environment.json says {_SENTINEL}", "confidence": 0.5, "should_use_decorator": None}
    )
    fake = create_autospec(judge._call_openrouter, spec_set=True, return_value=judge._TransportResult(leaked, "fake", 1, None))
    scope = _scope(tmp_path) if readonly else None
    response = judge.call_judge(_request(), transport_impl=fake, tool_scope=scope)
    assert _SENTINEL not in response.judge_rationale
    assert "provider-environment.json says" in response.judge_rationale


def test_readonly_scope_reads_evidence_but_never_the_provider_home(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence" / "root0"
    evidence.mkdir(parents=True)
    (evidence / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    provider = tmp_path / "provider"
    (provider / "codex").mkdir(parents=True)
    (provider / "provider-environment.json").write_text(json.dumps({"ANTHROPIC_API_KEY": _SENTINEL}), encoding="utf-8")
    (evidence / "escape.json").symlink_to(provider / "provider-environment.json")
    scope = _scope(evidence)

    allowed, _reason = judge._tool_scope_decision(scope, "Read", {"file_path": str(evidence / "module.py")})
    assert allowed is True
    for target in (provider / "provider-environment.json", provider / "codex", evidence / "escape.json"):
        allowed, reason = judge._tool_scope_decision(scope, "Read", {"file_path": str(target)})
        assert allowed is False, target
        assert _SENTINEL not in reason
    for tool in ("Bash", "Write", "Edit"):
        allowed, _reason = judge._tool_scope_decision(scope, tool, {"command": f"cat {provider}/provider-environment.json"})
        assert allowed is False, tool
