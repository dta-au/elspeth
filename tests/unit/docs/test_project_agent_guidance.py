"""Static contracts for project-local agent guidance and hook configuration."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
JUDGE_SKILLS = (
    REPO_ROOT / ".agents" / "skills" / "judge-signature-workflow" / "SKILL.md",
    REPO_ROOT / ".claude" / "skills" / "judge-signature-workflow" / "SKILL.md",
)
CICD_AUDIT_SKILL = REPO_ROOT / ".agents" / "skills" / "cicd-allowlist-audit" / "SKILL.md"
CONFIG_CONTRACTS_SKILL = REPO_ROOT / ".agents" / "skills" / "config-contracts-guide" / "SKILL.md"
AWS_SKILL_DIR = REPO_ROOT / ".agents" / "skills" / "operating-aws-ecs-container"
AGENTS_GUIDE = REPO_ROOT / "AGENTS.md"
CLAUDE_GUIDE = REPO_ROOT / "CLAUDE.md"
MAINTAINER_TOOLCHAIN = REPO_ROOT / "docs" / "maintainer" / "toolchain.md"

# The elspeth-lints flags that make a judge run safe to hand to an agent-staged
# bundle: read-only judge tools and a dry-run preview. Which *transport* runs
# the judge is the operator's choice, so the tests validate any named transport
# against the CLI's own argparse choices instead of pinning one harness.
_JUDGE_TRANSPORT_FLAG = re.compile(r"--judge-transport\s+(?P<value>\S+)")
_JUDGE_TOOLS_FLAG = re.compile(r"--judge-tools\s+(?P<value>\S+)")


def _judge_cli_choices(option: str) -> tuple[str, ...]:
    from elspeth_lints.core.cli import _build_parser

    for action in _build_parser()._actions:  # argparse exposes subparsers only via _actions
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for subparser in action.choices.values():
            for sub_action in subparser._actions:
                if option in sub_action.option_strings and sub_action.choices is not None:
                    return tuple(str(choice) for choice in sub_action.choices)
    raise AssertionError(f"{option} has no argparse choices on any elspeth-lints subcommand")


def _assert_judge_flags_are_readonly_and_valid(text: str, *, require_dry_run: bool) -> None:
    transports = [match.group("value").rstrip("`.,") for match in _JUDGE_TRANSPORT_FLAG.finditer(text)]
    tools = [match.group("value").rstrip("`.,") for match in _JUDGE_TOOLS_FLAG.finditer(text)]

    assert tools, "the skill never names --judge-tools"
    assert set(tools) == {"readonly"}, tools
    assert set(tools) <= set(_judge_cli_choices("--judge-tools"))
    assert set(transports) <= set(_judge_cli_choices("--judge-transport")), transports
    if require_dry_run:
        assert "--dry-run" in text


def _settings() -> dict[str, Any]:
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))


def _agent_guidance_text() -> str:
    """Every tracked file an installer may write a standing-instruction block into."""
    return "\n".join(path.read_text(encoding="utf-8") for path in (AGENTS_GUIDE, CLAUDE_GUIDE, MAINTAINER_TOOLCHAIN))


def _mcp_servers() -> dict[str, Any]:
    """`.mcp.json` is gitignored (local MCP wiring); an absent file wires no server."""
    path = REPO_ROOT / ".mcp.json"
    if not path.exists():
        return {}
    servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
    assert isinstance(servers, dict)
    return dict(servers)


def _hook_commands(settings: dict[str, Any]) -> list[str]:
    return [
        hook["command"]
        for groups in settings.get("hooks", {}).values()
        for group in groups
        for hook in group.get("hooks", [])
        if "command" in hook
    ]


def test_repository_hooks_bind_to_claude_project_dir() -> None:
    commands = _hook_commands(_settings())
    repository_commands = [command for command in commands if "loomweave" in command]

    assert repository_commands
    assert all("${CLAUDE_PROJECT_DIR}" in command for command in repository_commands)
    assert all("/home/john/elspeth" not in command for command in commands)


def test_hook_timeouts_are_bounded_seconds() -> None:
    settings = _settings()
    timeouts = [
        hook["timeout"]
        for groups in settings.get("hooks", {}).values()
        for group in groups
        for hook in group.get("hooks", [])
        if "timeout" in hook
    ]

    assert timeouts
    assert all(isinstance(timeout, int) and 0 < timeout <= 30 for timeout in timeouts)


WARDLINE_SURFACES = (
    REPO_ROOT / "weft.toml",
    REPO_ROOT / "scripts" / "wardline_pack.py",
    REPO_ROOT / ".agents" / "skills" / "wardline-gate",
    REPO_ROOT / ".claude" / "skills" / "wardline-gate",
)


def test_wardline_is_not_wired_into_the_project() -> None:
    """Wardline was rolled back on 2026-08-29 (ADR-043) and must not leak back in.

    It arrived through a Loomweave upgrade, and ``wardline install`` rewrites
    its own AGENTS.md block, skill copies, and ``weft.toml`` on every run — so a
    sibling-tool upgrade can silently re-add the whole surface. Pin the
    absence of every file and instruction the installer writes, plus the MCP
    server entry, so a re-leak fails here instead of reappearing as a standing
    agent instruction. Historical plans, specs, and ledgers may still mention
    Wardline; the live guidance and configuration may not.
    """
    agents_text = _agent_guidance_text()
    assert "wardline:instructions" not in agents_text
    assert "wardline scan" not in agents_text
    assert "scripts.wardline_pack" not in agents_text
    assert "mcp__wardline__" not in agents_text

    mcp_servers = _mcp_servers()
    assert "wardline" not in mcp_servers

    for path in WARDLINE_SURFACES:
        assert not path.exists(), path


LEGIS_SURFACES = (
    REPO_ROOT / ".agents" / "skills" / "legis-workflow",
    REPO_ROOT / ".claude" / "skills" / "legis-workflow",
)


def test_legis_is_not_wired_into_the_project() -> None:
    """Legis was retired 2026-08-29 (ADR-043): a generic twin of the elspeth-judge seam.

    Its installer writes an AGENTS.md block, two skill copies, an MCP server and
    a SessionStart hook; pin the absence of each so a sibling-tool upgrade
    cannot re-add it silently.
    """
    agents_text = _agent_guidance_text()
    assert "legis:instructions" not in agents_text
    assert "mcp__legis__" not in agents_text

    mcp_servers = _mcp_servers()
    assert "legis" not in mcp_servers

    assert not any("legis" in command for command in _hook_commands(_settings()))

    for path in LEGIS_SURFACES:
        assert not path.exists(), path


WARPLINE_SURFACES = (
    REPO_ROOT / ".agents" / "skills" / "warpline-workflow",
    REPO_ROOT / ".claude" / "skills" / "warpline-workflow",
)


def test_warpline_is_not_wired_into_the_project() -> None:
    """Warpline was retired 2026-08-29 (ADR-043): its only ingestion path was a
    per-clone post-commit hook, so worktree-authored commits and merges — how
    this project lands work — were never recorded and its answers were
    confidently incomplete. Pin the absence of every installer-written surface.
    """
    agents_text = _agent_guidance_text()
    assert "warpline:instructions" not in agents_text
    assert "mcp__warpline__" not in agents_text

    mcp_servers = _mcp_servers()
    assert "warpline" not in mcp_servers

    assert not any("warpline" in command for command in _hook_commands(_settings()))

    for path in WARPLINE_SURFACES:
        assert not path.exists(), path


def test_judge_skill_copies_use_readonly_dry_run_signing() -> None:
    for path in JUDGE_SKILLS:
        text = path.read_text(encoding="utf-8")
        _assert_judge_flags_are_readonly_and_valid(text, require_dry_run=True)
        assert "**`stage_scan`**" in text


def test_cicd_audit_routes_signed_row_changes_through_staging() -> None:
    text = CICD_AUDIT_SKILL.read_text(encoding="utf-8")

    assert "mcp__elspeth-judge__stage_scan" in text
    assert "stale_delete" in text
    _assert_judge_flags_are_readonly_and_valid(text, require_dry_run=True)
    assert "remove that stale row" not in text


def test_maintainer_toolchain_doc_is_labelled_not_required() -> None:
    text = MAINTAINER_TOOLCHAIN.read_text(encoding="utf-8")

    assert text.startswith("# Maintainer toolchain\n")
    assert "not a requirement of the project" in text.split("\n## ", maxsplit=1)[0]
    assert "<!-- loomweave:instructions:" in text and "<!-- /loomweave:instructions -->" in text
    assert "## Standing authorization: skills, subagents, and workflows" in text
    assert "lane-manager" in text
    assert "/home/john" not in text


def test_config_contracts_routes_new_entries_through_staging() -> None:
    text = CONFIG_CONTRACTS_SKILL.read_text(encoding="utf-8")

    assert "mcp__elspeth-judge__stage_scan" in text
    assert "new_judgment" in text
    assert "add the entry to that module's YAML file under `allow_hits:`" not in text


def test_aws_worktree_guidance_never_syncs_the_shared_venv() -> None:
    paths = (AWS_SKILL_DIR / "SKILL.md", *sorted((AWS_SKILL_DIR / "references").glob("*.md")))
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert not re.search(r"(?m)^\s*uv sync\b", text)
    assert not re.search(r"(?m)^\s*uv run\b", text)
    assert "PYTHONPATH" in text
    assert ".venv/bin/pytest" in text


def test_cicd_audit_uses_the_selected_checkout() -> None:
    text = CICD_AUDIT_SKILL.read_text(encoding="utf-8")

    assert "/home/john/elspeth" not in text
    assert "git rev-parse --show-toplevel" in text


def test_cicd_audit_lint_commands_import_from_the_selected_checkout() -> None:
    text = CICD_AUDIT_SKILL.read_text(encoding="utf-8")
    bash_blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)
    lint_blocks = [block for block in bash_blocks if "elspeth_lints.core.cli" in block]

    assert len(lint_blocks) == 3
    assert ".venv/bin/elspeth-lints" not in text
    assert not re.search(r"(?m)^\s*elspeth-lints(?:\s|$)", text)
    for block in lint_blocks:
        assert "REPO_ROOT=$(git rev-parse --show-toplevel)" in block
        assert 'PYTHONPATH="$REPO_ROOT/elspeth-lints/src"' in block
        assert ".venv/bin/python -m elspeth_lints.core.cli" in block


def test_tracked_handovers_never_direct_hook_bypass() -> None:
    for path in sorted((REPO_ROOT / ".claude" / "handovers").glob("*.md")):
        assert "--no-verify" not in path.read_text(encoding="utf-8"), path


def test_local_tool_state_has_only_narrow_project_ignores() -> None:
    lines = {
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".codex",
        ".antigravitycli/",
        ".claude/settings.local.json",
        ".claude/scheduled_tasks.lock",
        ".claude/worktrees/",
        ".claude/plans/",
        ".agents/skills/loomweave-workflow/.fingerprint",
        ".claude/skills/loomweave-workflow/.fingerprint",
        ".agents/skills/loomweave-workflow/SKILL.md",
        ".claude/skills/loomweave-workflow/SKILL.md",
    } <= lines
    assert ".claude/" not in lines
    assert ".agents/" not in lines
