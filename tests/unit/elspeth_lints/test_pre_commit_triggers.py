"""Pre-commit trigger contracts for the elspeth-lints hooks.

Each hook's ``files:`` regex is the ONLY thing scoping a ``--files`` run:
``--root`` never narrows an explicit file list (``ast_walker.iter_python_files``
yields every passed path verbatim). A trigger wider than a rule's
``path_filter`` fails loudly (``--fail-on-inert`` / out-of-scope refusal); a
trigger narrower than it is silent — pre-commit prints ``Skipped``. These
tests hunt the silent direction, and the two shapes that hide behind it:
a whole-repo rule wearing a changed-files label, and an allowlist that
re-runs nothing when edited.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
CICD_CONFIG_DIR = REPO_ROOT / "config" / "cicd"


@dataclass(frozen=True, slots=True)
class LintHook:
    hook_id: str
    entry: str
    rule_ids: tuple[str, ...]
    root: Path
    passes_files: bool
    files_pattern: re.Pattern[str]
    exclude_pattern: re.Pattern[str] | None
    types: tuple[str, ...]


def _lint_hooks() -> list[LintHook]:
    from elspeth_lints.core.cli import _expand_rule_tokens, _parse_rules
    from elspeth_lints.core.registry import RuleRegistry

    registry = RuleRegistry()
    registry.load_builtin_rules()
    available = set(registry.ids())
    payload = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    hooks: list[LintHook] = []
    for repo in payload["repos"]:
        for hook in repo.get("hooks", ()):
            entry = hook.get("entry", "")
            if "elspeth_lints.core.cli check" not in entry:
                continue
            argv = shlex.split(entry)
            check_args = argv[argv.index("check") + 1 :]
            rule_ids = _expand_rule_tokens(_parse_rules(check_args[check_args.index("--rules") + 1]), available)
            root = Path(check_args[check_args.index("--root") + 1]) if "--root" in check_args else Path(".")
            exclude = hook.get("exclude")
            hooks.append(
                LintHook(
                    hook_id=hook["id"],
                    entry=entry,
                    rule_ids=tuple(sorted(rule_ids)),
                    root=root,
                    passes_files="--files" in check_args,
                    files_pattern=re.compile(hook.get("files", "")),
                    exclude_pattern=re.compile(exclude) if exclude else None,
                    types=tuple(hook.get("types", ())),
                )
            )
    assert hooks, "no elspeth-lints check hooks found in .pre-commit-config.yaml"
    return hooks


def _hook_triggers_on(hook: LintHook, repo_relative: str) -> bool:
    """Mirror pre-commit's selection: ``files`` AND NOT ``exclude`` AND ``types``."""
    if hook.types and "python" in hook.types and not repo_relative.endswith(".py"):
        return False
    if hook.exclude_pattern is not None and hook.exclude_pattern.search(repo_relative):
        return False
    return hook.files_pattern.search(repo_relative) is not None


def _tracked_paths() -> list[str]:
    import subprocess

    output = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True, capture_output=True).stdout
    return [entry.decode() for entry in output.split(b"\0") if entry]


@pytest.fixture(scope="module")
def lint_hooks() -> list[LintHook]:
    return _lint_hooks()


@pytest.fixture(scope="module")
def tracked_paths() -> list[str]:
    return _tracked_paths()


def test_changed_files_hooks_select_only_incremental_rules(lint_hooks: list[LintHook]) -> None:
    """``--files`` only narrows the prewalk; a WHOLE_REPO rule ignores it and rescans the root.

    Such a hook is a whole-repo scan behind a subject-code trigger the header
    contract does not sanction, and its ``--files`` argument is inert.
    """
    from elspeth_lints.core.protocols import RuleScope
    from elspeth_lints.core.registry import RuleRegistry

    registry = RuleRegistry()
    registry.load_builtin_rules()
    offenders = {
        hook.hook_id: [rule_id for rule_id in hook.rule_ids if registry.get(rule_id).scope is not RuleScope.INCREMENTAL]
        for hook in lint_hooks
        if hook.passes_files
    }
    assert {hook_id: rules for hook_id, rules in offenders.items() if rules} == {}


def test_changed_files_hook_triggers_cover_every_file_their_rules_claim(
    lint_hooks: list[LintHook],
    tracked_paths: list[str],
) -> None:
    """Every tracked file a hook's incremental rules would judge under ``--root`` must also trip its trigger.

    The walker is the same one the CLI scans with, so exclusions (worktrees,
    venvs) and lint-rule fixtures are judged exactly as the gate judges them.
    """
    from elspeth_lints.core.ast_walker import iter_python_files
    from elspeth_lints.core.cli import _is_lint_rule_fixture_path, _path_matches_rule
    from elspeth_lints.core.registry import RuleRegistry

    registry = RuleRegistry()
    registry.load_builtin_rules()
    tracked = set(tracked_paths)
    gaps: dict[str, list[str]] = {}
    for hook in lint_hooks:
        if not hook.passes_files:
            continue
        rules = [registry.get(rule_id) for rule_id in hook.rule_ids]
        root = REPO_ROOT / hook.root
        missed: list[str] = []
        for file_path in iter_python_files(root):
            repo_relative = file_path.relative_to(REPO_ROOT).as_posix()
            if repo_relative not in tracked or _is_lint_rule_fixture_path(file_path):
                continue
            if not any(_path_matches_rule(file_path, root=root, rule=rule) for rule in rules):
                continue
            if not _hook_triggers_on(hook, repo_relative):
                missed.append(repo_relative)
        if missed:
            gaps[hook.hook_id] = missed
    assert gaps == {}


def _all_hooks() -> list[LintHook]:
    """Every pre-commit hook (any language, any stage) as a trigger-only record."""
    payload = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    hooks: list[LintHook] = []
    for repo in payload["repos"]:
        for hook in repo.get("hooks", ()):
            exclude = hook.get("exclude")
            hooks.append(
                LintHook(
                    hook_id=hook["id"],
                    entry=hook.get("entry", ""),
                    rule_ids=(),
                    root=Path("."),
                    passes_files=False,
                    files_pattern=re.compile(hook.get("files", "")),
                    exclude_pattern=re.compile(exclude) if exclude else None,
                    types=tuple(hook.get("types", ())),
                )
            )
    return hooks


def _textual_consumers(directory: str) -> list[str]:
    """Files outside pre-commit's file triggers that name the directory explicitly.

    A CI-only workflow or the commit-msg script may be a directory's sole
    consumer; a rule that loads its allowlist implicitly (by convention, not
    by path) leaves no such trace and must be reached through a hook trigger.
    """
    candidates = [*sorted((REPO_ROOT / ".github" / "workflows").glob("*.yaml")), *sorted((REPO_ROOT / ".githooks").iterdir())]
    return [
        path.relative_to(REPO_ROOT).as_posix() for path in candidates if path.is_file() and directory in path.read_text(encoding="utf-8")
    ]


def test_every_cicd_config_directory_has_a_consumer_that_reruns(tracked_paths: list[str]) -> None:
    """Editing ``config/cicd/<name>/`` must re-run something: a hook trigger, a CI workflow, or the commit-msg script.

    Pre-commit ANDs ``files`` with ``types``, so a ``types: [python]`` hook
    never fires on a YAML allowlist; only a trigger that names the directory
    counts.
    """
    hooks = _all_hooks()
    blind: dict[str, list[str]] = {}
    for config_dir in sorted(path for path in CICD_CONFIG_DIR.iterdir() if path.is_dir()):
        prefix = config_dir.relative_to(REPO_ROOT).as_posix() + "/"
        members = [path for path in tracked_paths if path.startswith(prefix)]
        if not members:
            continue
        if _textual_consumers(prefix.rstrip("/")):
            continue
        untriggered = [path for path in members if not any(_hook_triggers_on(hook, path) for hook in hooks)]
        if untriggered:
            blind[prefix] = untriggered
    assert blind == {}
