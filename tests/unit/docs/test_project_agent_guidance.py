"""Static contracts for project-local agent guidance and hook configuration."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
WARDLINE_SKILLS = (
    REPO_ROOT / ".agents" / "skills" / "wardline-gate" / "SKILL.md",
    REPO_ROOT / ".claude" / "skills" / "wardline-gate" / "SKILL.md",
)
JUDGE_SKILLS = (
    REPO_ROOT / ".agents" / "skills" / "judge-signature-workflow" / "SKILL.md",
    REPO_ROOT / ".claude" / "skills" / "judge-signature-workflow" / "SKILL.md",
)
CICD_AUDIT_SKILL = REPO_ROOT / ".agents" / "skills" / "cicd-allowlist-audit" / "SKILL.md"
CONFIG_CONTRACTS_SKILL = REPO_ROOT / ".agents" / "skills" / "config-contracts-guide" / "SKILL.md"
AWS_SKILL_DIR = REPO_ROOT / ".agents" / "skills" / "operating-aws-ecs-container"


def _settings() -> dict[str, Any]:
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))


def _hook_commands(settings: dict[str, Any]) -> list[str]:
    return [
        hook["command"]
        for groups in settings.get("hooks", {}).values()
        for group in groups
        for hook in group.get("hooks", [])
        if "command" in hook
    ]


def test_post_edit_hooks_do_not_mutate_python_files() -> None:
    post_tool_use = _settings().get("hooks", {}).get("PostToolUse", [])
    edit_commands = [
        hook["command"]
        for group in post_tool_use
        if re.search(r"(?:^|\|)(?:Edit|Write)(?:\||$)", group.get("matcher", ""))
        for hook in group.get("hooks", [])
        if "command" in hook
    ]

    assert edit_commands == []


def test_repository_hooks_bind_to_claude_project_dir() -> None:
    commands = _hook_commands(_settings())
    repository_commands = [command for command in commands if "loomweave" in command or "warpline" in command]

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


def test_wardline_skill_copies_use_the_project_gate() -> None:
    for path in WARDLINE_SKILLS:
        text = path.read_text(encoding="utf-8")
        assert ".venv/bin/python scripts/wardline_gate.py" in text
        assert "wardline scan . --fail-on ERROR" not in text


def test_judge_skill_copies_use_codex_readonly_signing() -> None:
    for path in JUDGE_SKILLS:
        text = path.read_text(encoding="utf-8")
        assert "--judge-transport codex-cli --judge-tools readonly --dry-run" in text
        assert "**`stage_scan`**" in text


def test_cicd_audit_routes_signed_row_changes_through_staging() -> None:
    text = CICD_AUDIT_SKILL.read_text(encoding="utf-8")

    assert "mcp__elspeth-judge__stage_scan" in text
    assert "stale_delete" in text
    assert "--judge-transport codex-cli --judge-tools readonly --dry-run" in text
    assert "remove that stale row" not in text


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
