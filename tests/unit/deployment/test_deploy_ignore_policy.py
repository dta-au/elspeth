"""Regression tests for deployment artifact ignore policy."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[3]


def _is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", "--", path],
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode in {0, 1}
    return result.returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        "deploy/platforms/aws-ecs.yaml",
        "deploy/compose/postgres.yaml",
        "deploy/linux-systemd/elspeth-web.service",
        "deploy/azure-container-apps/main.bicep",
        "deploy/kubernetes/base/deployment.yaml",
    ],
)
def test_shipped_deployment_artifacts_are_not_ignored(path: str) -> None:
    assert _is_ignored(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "deploy/elspeth-web.env",
        "deploy/compose/.env",
        "deploy/compose/operator.local.yaml",
        "deploy/kubernetes/base/secret.local.yaml",
        "deploy/Caddyfile",
        "deploy/elspeth-web.service.bak-20260724",
    ],
)
def test_local_deployment_secrets_and_overrides_stay_ignored(path: str) -> None:
    assert _is_ignored(path) is True


def test_example_environment_file_is_trackable() -> None:
    assert _is_ignored("deploy/compose/.env.example") is False


# ---------------------------------------------------------------------------
# .dockerignore
#
# Docker does NOT use gitignore semantics. Each pattern is matched with Go's
# ``filepath.Match`` against the CONTEXT-RELATIVE path, anchored at the
# context root: ``*.db`` excludes only a top-level ``foo.db``, and ``worktrees/``
# only a top-level ``./worktrees``. A pattern matches any depth only when it
# is written ``**/``-prefixed. A trailing ``/`` is stripped; a pattern that
# matches a directory excludes everything beneath it; a later ``!`` pattern
# re-includes. The matcher below honours exactly those rules so the test
# exercises the file instead of ``git check-ignore``.
# ---------------------------------------------------------------------------

DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _docker_pattern_regex(pattern: str) -> str:
    parts = pattern.strip("/").split("/")
    pieces: list[str] = []
    for index, part in enumerate(parts):
        if part == "**":
            pieces.append("(?:[^/]+/)*" if index < len(parts) - 1 else ".*")
            continue
        out = ""
        for char in part:
            if char == "*":
                out += "[^/]*"
            elif char == "?":
                out += "[^/]"
            elif char in "[]":
                out += char
            else:
                out += __import__("re").escape(char)
        pieces.append(out + ("/" if index < len(parts) - 1 else ""))
    return "".join(pieces)


def _docker_ignored(path: str, patterns: list[str]) -> bool:
    import re

    ancestors = [path] + ["/".join(path.split("/")[:count]) for count in range(1, path.count("/") + 1)]
    ignored = False
    for raw in patterns:
        negate = raw.startswith("!")
        regex = re.compile(_docker_pattern_regex(raw[1:] if negate else raw) + "$")
        if any(regex.fullmatch(candidate) for candidate in ancestors):
            ignored = not negate
    return ignored


def _dockerignore_patterns() -> list[str]:
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def test_docker_matcher_is_anchored_and_only_double_star_recurses() -> None:
    """Pin the semantics difference so a gitignore-style reading cannot pass by accident."""
    assert _docker_ignored("foo.db", ["*.db"]) is True
    assert _docker_ignored("a/foo.db", ["*.db"]) is False
    assert _docker_ignored("a/b/foo.db", ["**/*.db"]) is True
    assert _docker_ignored("worktrees/x/y.py", ["worktrees/"]) is True
    assert _docker_ignored(".claude/worktrees/x/y.py", ["worktrees/"]) is False
    assert _docker_ignored("a/b.pem", ["**/*.pem", "!a/b.pem"]) is False


@pytest.mark.parametrize(
    "path",
    [
        ".claude/worktrees/wt/src/elspeth/__init__.py",
        ".claude/worktrees/wt/.env",
        ".claude/worktrees/wt/deploy/compose/.env",
        ".claude/worktrees/wt/landscape.db",
        ".claude/worktrees/nested/.claude/worktrees/inner/src/x.py",
        ".worktrees/panel/src/elspeth/__init__.py",
        "src/elspeth/.env",
        "src/elspeth/web/private.key",
        "src/elspeth/web/server.pem",
        "src/elspeth/__pycache__/x.cpython-312.pyc",
        "src/elspeth/fixtures/test.db",
        "src/elspeth/web/frontend/src/App.test.tsx",
        "tests/unit/test_x.py",
        "docs/index.md",
        ".git/HEAD",
    ],
)
def test_docker_context_excludes(path: str) -> None:
    assert _docker_ignored(path, _dockerignore_patterns()) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/elspeth/__init__.py",
        "src/elspeth/web/secrets/service.py",
        "src/elspeth/web/frontend/src/App.tsx",
        "src/elspeth/web/frontend/package-lock.json",
        "elspeth-lints/src/elspeth_lints/__init__.py",
        "deploy/aws-ecs/trust/global-bundle.pem",
        "deploy/aws-ecs/trust/global-bundle.pem.sha256",
        "pyproject.toml",
        "uv.lock",
        "README.md",
    ],
)
def test_docker_context_keeps_what_the_dockerfile_copies(path: str) -> None:
    assert _docker_ignored(path, _dockerignore_patterns()) is False


def test_every_dockerfile_copy_source_survives_the_context() -> None:
    """Derive the keep-list from the Dockerfile rather than restating it."""
    import re

    patterns = _dockerignore_patterns()
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True, capture_output=True).stdout
    tracked_paths = [entry.decode() for entry in tracked.split(b"\0") if entry]
    dropped: dict[str, list[str]] = {}
    for line in (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*COPY\s+(?!--from)(?:--\S+\s+)*(.+?)\s+\S+\s*$", line)
        if match is None:
            continue
        for source in match.group(1).split():
            prefix = source.rstrip("/")
            members = [path for path in tracked_paths if path == prefix or path.startswith(prefix + "/")]
            assert members, f"Dockerfile COPY source {source!r} matches no tracked file"
            lost = [path for path in members if _docker_ignored(path, patterns) and not path.endswith((".md", ".test.ts", ".test.tsx"))]
            if lost:
                dropped[source] = lost
    assert dropped == {}
