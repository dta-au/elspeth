"""Tests for the reverted-guard meta-check.

The failure mode under test: a commit lands a production guard plus a test,
a later file-level restore or merge silently reverts the guard, and the test
survives (green for the wrong reason, or quietly failing in a subset nobody
runs). The detector compares each recent commit's *added* production lines
against HEAD and flags commits whose production additions vanished while
their test additions survived.

Precision over recall is the contract: every threshold test here pins a
condition that suppresses a would-be finding.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts.red_team import meta_check

SAMPLE_DIFF = """\
diff --git a/src/elspeth/web/auth/tokens.py b/src/elspeth/web/auth/tokens.py
index 111..222 100644
--- a/src/elspeth/web/auth/tokens.py
+++ b/src/elspeth/web/auth/tokens.py
@@ -10,0 +11,2 @@ def check(token):
+    if token.expired:
+        raise TokenExpiredError(token.id)
diff --git a/tests/unit/web/test_tokens.py b/tests/unit/web/test_tokens.py
index 333..444 100644
--- a/tests/unit/web/test_tokens.py
+++ b/tests/unit/web/test_tokens.py
@@ -5,0 +6,3 @@
+def test_expired_token_rejected():
+    with pytest.raises(TokenExpiredError):
+        check(make_token(expired=True))
"""


class TestParseAddedLines:
    def test_maps_paths_to_added_lines(self) -> None:
        added = meta_check.parse_added_lines(SAMPLE_DIFF)
        assert set(added) == {
            "src/elspeth/web/auth/tokens.py",
            "tests/unit/web/test_tokens.py",
        }
        assert "        raise TokenExpiredError(token.id)" in added["src/elspeth/web/auth/tokens.py"]
        assert len(added["tests/unit/web/test_tokens.py"]) == 3

    def test_ignores_removed_and_context_lines(self) -> None:
        diff = "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n@@ -1,2 +1,1 @@\n-removed = 1\n context = 2\n"
        assert meta_check.parse_added_lines(diff) == {}


class TestSubstantiveLines:
    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "# just a comment",
            "    )",
            "]",
            "},",
            "    pass",
        ],
    )
    def test_trivial_lines_rejected(self, line: str) -> None:
        assert meta_check.is_substantive(line) is False

    @pytest.mark.parametrize(
        "line",
        [
            "    if token.expired:",
            "        raise TokenExpiredError(token.id)",
            "    return _fail_closed(reason)",
        ],
    )
    def test_code_lines_accepted(self, line: str) -> None:
        assert meta_check.is_substantive(line) is True


class TestSurvival:
    def test_counts_normalized_matches(self) -> None:
        added = ["    if token.expired:", "        raise TokenExpiredError(token.id)"]
        current = "def check(token):\n  if token.expired:\n    raise TokenExpiredError(token.id)\n"
        survived, total = meta_check.survival(added, current)
        assert (survived, total) == (2, 2)

    def test_missing_lines_counted_as_dead(self) -> None:
        added = ["    if token.expired:", "        raise TokenExpiredError(token.id)"]
        survived, total = meta_check.survival(added, "def check(token):\n    return True\n")
        assert (survived, total) == (0, 2)

    def test_trivial_lines_excluded_from_total(self) -> None:
        added = ["", "# comment", "    real_code = compute()"]
        _survived, total = meta_check.survival(added, "x = 1\n")
        assert total == 1


class TestAssess:
    def _stats(self, survived: int, total: int) -> meta_check.SurvivalStats:
        return meta_check.SurvivalStats(survived=survived, total=total)

    def test_flags_dead_guard_with_surviving_tests(self) -> None:
        verdict = meta_check.assess(prod=self._stats(0, 5), tests=self._stats(4, 4))
        assert verdict is True

    def test_ignores_when_prod_survives(self) -> None:
        assert meta_check.assess(prod=self._stats(5, 5), tests=self._stats(4, 4)) is False

    def test_ignores_partial_prod_survival_above_threshold(self) -> None:
        assert meta_check.assess(prod=self._stats(2, 5), tests=self._stats(4, 4)) is False

    def test_ignores_commit_without_test_additions(self) -> None:
        assert meta_check.assess(prod=self._stats(0, 5), tests=self._stats(0, 0)) is False

    def test_ignores_when_tests_also_died(self) -> None:
        assert meta_check.assess(prod=self._stats(0, 5), tests=self._stats(1, 4)) is False

    def test_requires_minimum_prod_lines(self) -> None:
        assert meta_check.assess(prod=self._stats(0, 2), tests=self._stats(3, 3)) is False


class TestPathClassification:
    def test_test_paths(self) -> None:
        assert meta_check.is_test_path("tests/unit/web/test_tokens.py") is True
        assert meta_check.is_test_path("src/elspeth/web/auth/tokens.py") is False

    def test_prod_paths(self) -> None:
        assert meta_check.is_prod_path("src/elspeth/web/auth/tokens.py") is True
        assert meta_check.is_prod_path("scripts/cicd/plugin_hash.py") is True
        assert meta_check.is_prod_path("tests/unit/web/test_tokens.py") is False
        assert meta_check.is_prod_path("docs/notes.md") is False


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src/elspeth/web/auth").mkdir(parents=True)
    (repo / "tests/unit/web").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "red-team@test.invalid")
    _git(repo, "config", "user.name", "red-team-test")
    _git(repo, "config", "commit.gpgsign", "false")

    guard_file = repo / "src/elspeth/web/auth/tokens.py"
    test_file = repo / "tests/unit/web/test_tokens.py"

    guard_file.write_text("def check(token):\n    return True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")

    guard_file.write_text(
        "def check(token):\n"
        "    if token.expired:\n"
        "        raise TokenExpiredError(token.id)\n"
        "    if token.audience != EXPECTED_AUDIENCE:\n"
        "        raise TokenAudienceError(token.id)\n"
        "    return True\n"
    )
    test_file.write_text(
        "def test_expired_token_rejected():\n    with pytest.raises(TokenExpiredError):\n        check(make_token(expired=True))\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix: reject expired and wrong-audience tokens")
    return repo


class TestCheckCommitAgainstRealRepo:
    def test_detects_reverted_guard_with_surviving_test(self, sample_repo: Path) -> None:
        fix_sha = _git(sample_repo, "rev-parse", "HEAD")
        # File-level restore: guard reverted, test survives.
        (sample_repo / "src/elspeth/web/auth/tokens.py").write_text("def check(token):\n    return True\n")
        _git(sample_repo, "add", "-A")
        _git(sample_repo, "commit", "-q", "-m", "restore tokens.py")

        finding = meta_check.check_commit(fix_sha, repo_root=sample_repo)
        assert finding is not None
        assert finding.commit == fix_sha
        assert "src/elspeth/web/auth/tokens.py" in finding.dead_prod_files
        assert "tests/unit/web/test_tokens.py" in finding.surviving_test_files

    def test_no_finding_when_fix_intact(self, sample_repo: Path) -> None:
        fix_sha = _git(sample_repo, "rev-parse", "HEAD")
        finding = meta_check.check_commit(fix_sha, repo_root=sample_repo)
        assert finding is None

    def test_no_finding_when_guard_file_deleted_entirely(self, sample_repo: Path) -> None:
        # Deleted (not restored) files are skipped for precision: a rename or
        # legitimate removal is indistinguishable from a revert by line match.
        fix_sha = _git(sample_repo, "rev-parse", "HEAD")
        _git(sample_repo, "rm", "-q", "src/elspeth/web/auth/tokens.py")
        _git(sample_repo, "commit", "-q", "-m", "remove tokens module")
        finding = meta_check.check_commit(fix_sha, repo_root=sample_repo)
        assert finding is None

    def test_scan_walks_recent_commits(self, sample_repo: Path) -> None:
        fix_sha = _git(sample_repo, "rev-parse", "HEAD")
        (sample_repo / "src/elspeth/web/auth/tokens.py").write_text("def check(token):\n    return True\n")
        _git(sample_repo, "add", "-A")
        _git(sample_repo, "commit", "-q", "-m", "restore tokens.py")

        findings = meta_check.scan(last=10, repo_root=sample_repo)
        assert [f.commit for f in findings] == [fix_sha]
