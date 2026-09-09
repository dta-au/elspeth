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
# Every option the judge's argv may carry. Positive, so an unlisted flag fails
# by being unrecognised rather than by matching a prefix someone predicted.
_EXPECTED_ARGV_FLAGS = frozenset(
    {
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "--model",
        "--json",
        "--color",
        "--output-schema",
        "--cd",
        "-c",
    }
)
# The complete set of MCP keys the transport may register, pinned so the
# exemption above cannot be used to smuggle a setting in. `.enabled=false`
# was a measured bypass before this existed.
_EXPECTED_MCP_KEYS = frozenset(
    {
        f"{_MCP_CONFIG_PREFIX}command",
        f"{_MCP_CONFIG_PREFIX}args",
        f"{_MCP_CONFIG_PREFIX}env",
        f"{_MCP_CONFIG_PREFIX}enabled_tools",
        f"{_MCP_CONFIG_PREFIX}required",
        f"{_MCP_CONFIG_PREFIX}default_tools_approval_mode",
    }
)


def _assert_judge_shell_is_enabled(command: list[str]) -> None:
    """Fail if the argv could turn a Codex shell capability off, in ANY spelling.

    Deliberately unfiltered: the settings are compared as a whole list, so a
    novel override form fails by being unrecognised rather than by matching a
    pattern someone thought to write down.
    """
    # POSITIVE flag allowlist. Every option the judge's argv may carry is named
    # here; anything else fails by being unrecognised.
    #
    # This replaces five `startswith` blocklists. A fifth adversarial review
    # (2026-09-09) pointed out that those were the very filter shape the comment
    # above declares unsound, and walked through them the same way its
    # predecessors walked through the config filters: `-p` is Codex's short
    # `--profile`, and `--enable <FEATURE>` is the documented `-c
    # features.<name>=true`, which was measured to override an explicit
    # `features.apps=false` in either order. `--add-dir` and `--oss` were also
    # unrefused. One positive assertion catches all of them, and every flag
    # Codex adds in future, at once.
    flags = {part for part in command if part.startswith("-") and part != "-"}
    assert flags <= _EXPECTED_ARGV_FLAGS, f"unrecognised flag on the judge's argv: {sorted(flags - _EXPECTED_ARGV_FLAGS)}"
    config_values = [command[index + 1] for index, part in enumerate(command) if part == "-c"]
    mcp_values = [value for value in config_values if value.startswith(_MCP_CONFIG_PREFIX)]
    base_values = [value for value in config_values if not value.startswith(_MCP_CONFIG_PREFIX)]
    assert tuple(base_values) == _EXPECTED_BASE_CONFIG, (
        f"the judge's Codex config changed; every -c value must be pinned here. unexpected: {sorted(set(base_values) - set(_EXPECTED_BASE_CONFIG))}"
    )
    # The MCP exemption above is a hole unless the exempted keys are themselves
    # pinned: a fourth review measured `mcp_servers.elspeth_judge_tools.enabled=false`
    # flipping the server off in `codex mcp list --json` while every other
    # assertion passed. Keys only — the values carry interpreter and root paths.
    mcp_keys = {value.split("=", 1)[0] for value in mcp_values}
    assert mcp_keys <= _EXPECTED_MCP_KEYS, f"unexpected MCP setting on the judge's reader: {sorted(mcp_keys - _EXPECTED_MCP_KEYS)}"

    # The judge's OTHER controls, each measured as a live bypass by the same
    # review. These are not shell settings, but each one blinds or unleashes
    # the judge just as effectively.
    #
    # --ignore-user-config was asserted ONLY in the blinded test, so a
    # CODEX_HOME config.toml could resolve shell_tool false in tool mode
    # (measured). --dangerously-bypass-approvals-and-sandbox removes the
    # read-only sandbox, which is the WRITE control the whole design rests on.
    assert "--ignore-user-config" in command, "a CODEX_HOME config.toml can disable the shell the judge depends on"
    assert "--ignore-rules" in command, "repo execpolicy rules must not reach the judge"
    assert command[command.index("--sandbox") : command.index("--sandbox") + 2] == ["--sandbox", "read-only"]


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


# The affordances the tool-mode prompt MUST carry. A fourth adversarial review
# (2026-09-09) changed one token — `_codex_prompt(request, tool_mode=False)` —
# and produced a byte-identical argv, a still-registered MCP server, a shell
# still enabled by the sandbox, and a judge that was simply never told any of
# it existed. That reproduces the exact starvation `543066e17` was written to
# cure, and NO argv assertion can ever detect it: the judge's sight is decided
# by the prompt, not by the command line. Pinned here positively.
_TOOL_MODE_PROMPT_AFFORDANCES = (
    "TOOL-AUGMENTED INVESTIGATION MODE",
    "read_file/grep_files/glob_files",
    "your own shell tool works read-only",
    "prefer `grep -n`",
    "CITE WHAT YOU READ",
)


def _captured_prompt(monkeypatch: pytest.MonkeyPatch, *, tool_scope: AgentToolScope | None) -> str:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["input"] = kwargs["input"]
        return subprocess.CompletedProcess(command, 0, stdout=_jsonl(), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _call_codex_cli(_request(), DEFAULT_CODEX_JUDGE_MODEL, 1024, tool_scope=tool_scope)
    prompt = captured["input"]
    assert isinstance(prompt, str)
    return prompt


def test_codex_tool_mode_prompt_tells_the_judge_it_can_investigate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool mode must hand the judge its affordances, not just enable them.

    Enabling the shell and registering the reader is not enough: a judge that
    is not told it has tools does not use them, and the failure mode is a
    BLOCK-PENDING that reads exactly like a bad rationale.
    """
    source_root = tmp_path / "src"
    source_root.mkdir()
    scope = AgentToolScope(allowed_roots=(source_root.resolve(),), cwd=source_root.resolve(), max_turns=5)

    prompt = _captured_prompt(monkeypatch, tool_scope=scope)

    missing = [marker for marker in _TOOL_MODE_PROMPT_AFFORDANCES if marker not in prompt]
    assert missing == [], f"the tool-mode prompt no longer tells the judge it can investigate: {missing}"


def test_codex_blinded_mode_prompt_carries_no_investigation_affordances(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contrast that keeps the tool-mode pin from being vacuous.

    If these markers were unconditional the sibling test above would pass even
    with tool mode switched off, which is precisely the mutant it exists to
    catch.
    """
    prompt = _captured_prompt(monkeypatch, tool_scope=None)

    present = [marker for marker in _TOOL_MODE_PROMPT_AFFORDANCES if marker in prompt]
    assert present == [], f"blinded mode must not advertise tools it does not have: {present}"


def test_judge_investigation_budget_is_a_loop_guard_not_a_ration() -> None:
    """The read budget is load-bearing and was pinned by nothing.

    `543066e17` raised these because 24 calls at 400 lines starved the judge
    into three consecutive false BLOCKs on a correct rationale ("could not read
    the named pinning tests within the available investigation budget"). Every
    test that builds an `AgentToolScope` passes `max_turns` explicitly, so the
    default is never otherwise exercised — a silent revert to 24/400 would
    restore the starvation with the whole suite green.
    """
    from elspeth_lints.core.judge import _AGENT_TOOL_MODE_DEFAULT_MAX_TURNS
    from elspeth_lints.mcp.codex_judge_tools import _MAX_READ_LINES

    assert _AGENT_TOOL_MODE_DEFAULT_MAX_TURNS >= 200, "a judge that runs out of calls mid-investigation emits a false BLOCK"
    assert _MAX_READ_LINES >= 2000, "400-line reads cost 8+ calls on a single 3000-line test file"


def test_codex_transport_timeout_allows_a_real_investigation() -> None:
    """`_CODEX_CLI_TIMEOUT_SECONDS` is referenced by no other test in the repo.

    A fourth adversarial review reverted it 600 -> 5 with the whole
    `tests/unit/elspeth_lints/` suite green. Every real judge call would then
    die as a JudgeTransportError, which reads as an infrastructure fault
    rather than as the deliberate throttle it would be.
    """
    from elspeth_lints.core.judge import _CODEX_CLI_TIMEOUT_SECONDS

    assert _CODEX_CLI_TIMEOUT_SECONDS >= 600, "a tool-using judge routinely takes minutes to investigate a deep call chain"
