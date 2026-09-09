"""Tests for the Codex CLI judge transport.

The transport is intentionally subprocess-based: it reuses the operator's
installed + authenticated ``codex`` CLI without adding an API-key dependency to
``elspeth-lints``.  Tests fake ``subprocess.run`` so CI never invokes a real
model or consumes operator credentials.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from elspeth_lints.core.judge import (
    CODEX_JUDGE_REASONING_EFFORT,
    DEFAULT_CODEX_JUDGE_MODEL,
    TRANSPORT_CODEX_CLI,
    AgentToolScope,
    JudgeConfigurationError,
    JudgeRequest,
    _call_codex_cli,
    call_judge,
)

# The COMPLETE set of Codex feature settings the judge transport is allowed to
# send, in order. The judge's shell being ON is a security property of
# `543066e17`, so it is pinned by asserting the WHOLE config list, unfiltered.
#
# Three adversarial reviews (2026-09-09) each defeated a weaker form, every
# bypass measured against the real CLI with `codex features list` reporting
# shell_tool false while the test stayed green:
#   1. `"features.shell_tool=false" not in command` — exact list membership,
#      beaten by requoting to `features.shell_tool="false"`.
#   2. a regex per argv element — beaten structurally by Codex's documented
#      `--disable shell_tool` (the feature name is a SEPARATE argv element),
#      and by the attached `-cfeatures.X=false` / `--config=features.X=false`.
#   3. a set-equality over elements containing `"features."` — beaten by the
#      TOML inline table `features={shell_tool=false,shell_snapshot=false}`,
#      which never types the dot and so was never even examined.
#
# The third review named the shared root cause: *a positive assertion over a
# negatively-filtered subset is still a negative assertion.* Any filter can be
# stepped around by a spelling the filter does not recognise. So there is no
# filter here — every `-c` value must appear in this list, and the only values
# exempt are the judge's own MCP registrations, which are checked separately.
_EXPECTED_BASE_CONFIG: tuple[str, ...] = (
    'approval_policy="never"',
    'web_search="disabled"',
    f'model_reasoning_effort="{CODEX_JUDGE_REASONING_EFFORT}"',
    "features.apps=false",
    "features.hooks=false",
    "features.goals=false",
    "features.memories=false",
    "features.multi_agent=false",
    "features.remote_plugin=false",
    "features.personality=false",
)
_MCP_CONFIG_PREFIX = "mcp_servers.elspeth_judge_tools."


def _assert_judge_shell_is_enabled(command: list[str]) -> None:
    """Fail if the argv could turn a Codex shell capability off, in ANY spelling.

    Deliberately unfiltered: the settings are compared as a whole list, so a
    novel override form fails by being unrecognised rather than by matching a
    pattern someone thought to write down.
    """
    # Every other way to set a config value is refused outright, so the
    # list comparison below cannot be sidestepped by an attached form.
    for part in command:
        assert not part.startswith("--disable"), f"--disable is Codex's documented equivalent of features.<name>=false: {part!r}"
        assert not part.startswith("--config"), f"config must be passed as a bare -c pair, not attached: {part!r}"
        assert part == "-c" or not part.startswith("-c"), f"attached -c value bypasses the config pin: {part!r}"
    config_values = [command[index + 1] for index, part in enumerate(command) if part == "-c"]
    mcp_values = [value for value in config_values if value.startswith(_MCP_CONFIG_PREFIX)]
    base_values = [value for value in config_values if not value.startswith(_MCP_CONFIG_PREFIX)]
    assert tuple(base_values) == _EXPECTED_BASE_CONFIG, (
        f"the judge's Codex config changed; every -c value must be pinned here. unexpected: {sorted(set(base_values) - set(_EXPECTED_BASE_CONFIG))}"
    )
    # Tool mode adds MCP registrations; blinded mode adds none. Neither may
    # smuggle a feature setting in under the MCP prefix.
    assert all("features" not in value for value in mcp_values), f"an MCP setting must not carry a feature override: {mcp_values}"


def _request() -> JudgeRequest:
    return JudgeRequest(
        file_path="core/x.py",
        rule_id="R1",
        symbol="f",
        fingerprint="abc",
        rationale="external call boundary",
        surrounding_code="def f(x):\n    return x.get('a')\n",
    )


def _jsonl(*, verdict: str = "ACCEPTED") -> str:
    payload = {
        "verdict": verdict,
        "rationale": "external boundary; absence is preserved",
        "confidence": 0.8,
        "should_use_decorator": None,
    }
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "item-1", "type": "agent_message", "text": json.dumps(payload)},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1234,
                "cached_input_tokens": 1000,
                "output_tokens": 42,
                "reasoning_output_tokens": 7,
            },
        },
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


def test_codex_cli_transport_isolated_blinded_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        schema_path = Path(command[command.index("--output-schema") + 1])
        captured["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(command, 0, stdout=_jsonl(), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("ELSPETH_JUDGE_METADATA_HMAC_KEY", "operator-hmac")
    monkeypatch.setenv("ELSPETH_JUDGE_OVERRIDE_TOKEN", "operator-override")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    result = _call_codex_cli(_request(), DEFAULT_CODEX_JUDGE_MODEL, 1024)

    assert result.raw_text.startswith('{"verdict": "ACCEPTED"')
    assert result.served_model_id == DEFAULT_CODEX_JUDGE_MODEL
    assert result.prompt_tokens_total == 1234
    assert result.prompt_tokens_cached == 1000

    command = captured["command"]
    assert command[:2] == ["codex", "exec"]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--sandbox") : command.index("--sandbox") + 2] == ["--sandbox", "read-only"]
    assert command[command.index("--model") : command.index("--model") + 2] == ["--model", DEFAULT_CODEX_JUDGE_MODEL]
    reasoning_setting = f'model_reasoning_effort="{CODEX_JUDGE_REASONING_EFFORT}"'
    assert reasoning_setting == 'model_reasoning_effort="high"'
    assert command.count(reasoning_setting) == 1
    assert sum(part.startswith("model_reasoning_effort=") for part in command) == 1
    # The native shell stays available under the read-only sandbox (operator
    # ruling 2026-09-09); blinded mode simply has nothing to look at because
    # it runs in an empty temporary directory.
    _assert_judge_shell_is_enabled(command)
    cd_target = Path(command[command.index("--cd") + 1])
    assert cd_target.name.startswith("elspeth-judge-codex-")
    assert 'web_search="disabled"' in command
    assert "features.apps=false" in command
    assert "features.hooks=false" in command
    assert "features.multi_agent=false" in command
    assert captured["schema"]["additionalProperties"] is False

    child_env = captured["kwargs"]["env"]
    assert child_env["PATH"] == "/usr/bin:/bin"
    assert "ELSPETH_JUDGE_METADATA_HMAC_KEY" not in child_env
    assert "ELSPETH_JUDGE_OVERRIDE_TOKEN" not in child_env
    assert "OPENROUTER_API_KEY" not in child_env
    assert "OPENAI_API_KEY" not in child_env
    assert "ANTHROPIC_API_KEY" not in child_env
    assert "AWS_SECRET_ACCESS_KEY" not in child_env
    assert captured["kwargs"]["input"]
    assert captured["kwargs"]["check"] is False


def test_codex_cli_readonly_mode_registers_only_scoped_mcp_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "src"
    allowlist_root = tmp_path / "allowlists"
    source_root.mkdir()
    allowlist_root.mkdir()
    scope = AgentToolScope(
        allowed_roots=(source_root.resolve(), allowlist_root.resolve()),
        cwd=source_root.resolve(),
        max_turns=5,
    )
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout=_jsonl(), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    _call_codex_cli(_request(), DEFAULT_CODEX_JUDGE_MODEL, 1024, tool_scope=scope)

    command = captured["command"]
    joined = "\n".join(command)
    assert "mcp_servers.elspeth_judge_tools.command=" in joined
    assert "elspeth_lints.mcp.codex_judge_tools" in joined
    assert str(source_root.resolve()) in joined
    assert str(allowlist_root.resolve()) in joined
    assert 'mcp_servers.elspeth_judge_tools.enabled_tools=["read_file", "grep_files", "glob_files"]' in joined
    assert "mcp_servers.elspeth_judge_tools.required=true" in command
    assert 'mcp_servers.elspeth_judge_tools.default_tools_approval_mode="approve"' in command
    # Tool mode runs IN the checkout with Codex's native read-only shell on:
    # the MCP reader is a supplement, not the judge's only pair of eyes.
    #
    # Pinned as an exact whitelist by the helper: see its comment for the two
    # measured bypasses that defeated the earlier negative assertions.
    _assert_judge_shell_is_enabled(command)
    assert command[command.index("--sandbox") : command.index("--sandbox") + 2] == ["--sandbox", "read-only"]
    assert command[command.index("--cd") + 1] == str(source_root.resolve())


def test_codex_cli_missing_binary_is_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("codex")

    monkeypatch.setattr(subprocess, "run", missing)

    with pytest.raises(JudgeConfigurationError, match="Codex CLI"):
        _call_codex_cli(_request(), DEFAULT_CODEX_JUDGE_MODEL, 1024)


def test_call_judge_codex_transport_uses_codex_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout=_jsonl(), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    response = call_judge(_request(), transport=TRANSPORT_CODEX_CLI)

    assert response.judge_transport == TRANSPORT_CODEX_CLI
    assert response.model_id == DEFAULT_CODEX_JUDGE_MODEL
    command = captured["command"]
    assert command[command.index("--model") : command.index("--model") + 2] == ["--model", DEFAULT_CODEX_JUDGE_MODEL]
