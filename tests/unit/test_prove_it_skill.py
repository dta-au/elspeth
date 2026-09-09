"""Contract tests for the prove-it skill: falsifiable assertions, fresh-worktree verification,
snapshot-roundtrip mutation checks, verdict files, and the Stop hook that refuses an unproven completion.

Real git repositories and real subprocesses throughout — the mechanism's whole point is that it
does not take anyone's word, so these tests do not take the mechanism's either.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / ".agents" / "skills" / "prove-it"
SCRIPT = SKILL_DIR / "prove_it.py"
HOOK = SKILL_DIR / "stop_hook.py"
SETTINGS = REPO_ROOT / ".claude" / "settings.json"

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}
SESSION = "sess-aaaa-1111"
OTHER_SESSION = "sess-bbbb-2222"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prove_it_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pi = _load_module()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=GIT_ENV).stdout


TEST_CMD = f"{sys.executable} -m pytest tests/test_target.py -q -p no:cacheprovider"  # RED is structural: pytest records it
GOOD_TEST = "def test_value():\n    assert open('src/target.py').read().strip() == 'VALUE = 2'\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main: VALUE = 1 with a test that wants VALUE = 2 (red). Branch `work` is where fixes land."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "src" / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    (root / "tests" / "test_target.py").write_text(GOOD_TEST, encoding="utf-8")
    (root / ".gitignore").write_text(".verify/\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base: red test")
    _git(root, "checkout", "-q", "-b", "work")
    return root


def _commit_fix(repo: Path) -> str:
    (repo / "src" / "target.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "fix: VALUE = 2")
    return _git(repo, "rev-parse", "HEAD").strip()


def _claim(repo: Path, *assertions: str, session: str = SESSION, text: str = "the bug is fixed") -> object:
    return pi.create_claim(repo=repo, session_id=session, claim=text, assertions=list(assertions))


def _verdicts(repo: Path) -> list[Path]:
    return sorted(p for p in (repo / ".verify").glob("*.md"))


def _worktrees(repo: Path) -> list[str]:
    return [line.split(" ", 1)[1] for line in _git(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]


# ----------------------------------------------------------------- claims


def test_claim_is_decomposed_into_typed_assertions_on_disk(repo: Path) -> None:
    sha = _commit_fix(repo)
    claim = _claim(
        repo,
        f"test: {TEST_CMD}",
        f"commit: {sha} on work",
        f"mutation: {TEST_CMD} :: src/target.py @ main",
        "file: src/target.py",
        f"exit0: {sys.executable} -c 'import sys; sys.exit(0)'",
        f"nonzero: {sys.executable} -c 'import sys; sys.exit(3)'",
    )
    path = repo / ".verify" / "claims" / f"{claim.claim_id}.json"
    assert path.is_file()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["session_id"] == SESSION
    assert raw["claim"] == "the bug is fixed"
    assert raw["branch"] == "work"
    assert [a["kind"] for a in raw["assertions"]] == ["test", "commit", "mutation", "file", "exit0", "nonzero"]
    assert raw["assertions"][1] == {"id": "A2", "kind": "commit", "sha": sha, "branch": "work"}
    assert raw["assertions"][2]["paths"] == ["src/target.py"] and raw["assertions"][2]["base"] == "main"
    assert raw["withdrawn"] is None


def test_malformed_assertions_are_refused(repo: Path) -> None:
    with pytest.raises(ValueError, match="kind"):
        _claim(repo, "wish: it works")
    with pytest.raises(ValueError, match="on <branch>"):
        _claim(repo, "commit: abc123")
    with pytest.raises(ValueError, match="at least one"):
        pi.create_claim(repo=repo, session_id=SESSION, claim="done", assertions=[])


# ----------------------------------------------------------------- fresh-worktree verification


def test_test_assertion_runs_in_a_fresh_worktree_at_the_branch_tip(repo: Path) -> None:
    _commit_fix(repo)
    claim = _claim(repo, f"test: {TEST_CMD}")
    verdict = pi.verify_claim(repo, claim.claim_id)
    assert verdict.verdict == pi.VERDICT_UNREVIEWED, verdict.results
    assert verdict.results[0].proven is True
    assert verdict.results[0].where != str(repo), "the working directory is never the instrument"
    assert set(_worktrees(repo)) == {str(repo)}, "fresh worktree must be removed"


def test_uncommitted_fix_is_caught_as_a_false_positive(repo: Path) -> None:
    """The test passes in the working directory (fix uncommitted) but the branch tip is still red."""
    (repo / "src" / "target.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert subprocess.run([sys.executable, "tests/test_target.py"], cwd=repo).returncode == 0, "cwd would say PASS"
    claim = _claim(repo, f"test: {TEST_CMD}")
    verdict = pi.verify_claim(repo, claim.claim_id)
    assert verdict.verdict == pi.VERDICT_FAIL
    assert verdict.results[0].proven is False
    assert "uncommitted" in verdict.results[0].evidence
    assert (repo / "src" / "target.py").read_text(encoding="utf-8") == "VALUE = 2\n", "working directory untouched"


def test_commit_assertion_uses_rev_parse_and_ancestry(repo: Path) -> None:
    sha = _commit_fix(repo)
    _git(repo, "checkout", "-q", "-b", "elsewhere", "main")
    (repo / "src" / "other.py").write_text("OTHER = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "unrelated")
    stray = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "work")
    claim = _claim(repo, f"commit: {sha} on work", f"commit: {stray} on work", "commit: 0000000000000000000000000000000000000000 on work")
    verdict = pi.verify_claim(repo, claim.claim_id)
    assert [r.proven for r in verdict.results] == [True, False, False]
    assert "not an ancestor" in verdict.results[1].evidence
    assert "does not resolve" in verdict.results[2].evidence
    assert verdict.verdict == pi.VERDICT_FAIL
    assert verdict.unproven == ["A2", "A3"]


def test_file_exit0_and_nonzero_assertions(repo: Path) -> None:
    _commit_fix(repo)
    (repo / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
    claim = _claim(
        repo,
        "file: src/target.py",
        "file: scratch.txt",
        f"exit0: {sys.executable} -c 'import sys; sys.exit(0)'",
        f"nonzero: {sys.executable} -c 'import sys; sys.exit(0)'",
    )
    verdict = pi.verify_claim(repo, claim.claim_id)
    assert [r.proven for r in verdict.results] == [True, False, True, False]
    assert "not at the tip" in verdict.results[1].evidence


# ----------------------------------------------------------------- mutation verification


def test_mutation_in_working_directory_reverts_only_the_fix_and_restores_it(repo: Path) -> None:
    """Uncommitted fix: revert only the fix paths in place, test must go red, restore bytes; unrelated edits survive."""
    (repo / "src" / "target.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "src" / "other.py").write_text("OTHER = 99  # unrelated, must survive\n", encoding="utf-8")
    claim = _claim(repo, f"mutation: {TEST_CMD} :: src/target.py")
    verdict = pi.verify_claim(repo, claim.claim_id)
    result = verdict.results[0]
    assert result.proven is True, result.evidence
    assert "snapshot" in result.evidence and "red" in result.evidence
    assert (repo / "src" / "target.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (repo / "src" / "other.py").read_text(encoding="utf-8") == "OTHER = 99  # unrelated, must survive\n"
    assert sorted(_git(repo, "status", "--porcelain").splitlines()) == [" M src/other.py", " M src/target.py"], "nothing else touched"


def test_mutation_that_does_not_go_red_is_unproven_and_still_restores(repo: Path) -> None:
    (repo / "src" / "target.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "tests" / "test_target.py").write_text("def test_nothing():\n    assert True\n", encoding="utf-8")  # cannot fail
    _git(repo, "commit", "-q", "-am", "test: neutered")
    claim = _claim(repo, f"mutation: {TEST_CMD} :: src/target.py")
    (repo / "src" / "target.py").write_text("VALUE = 2  # still uncommitted\n", encoding="utf-8")
    verdict = pi.verify_claim(repo, claim.claim_id)
    assert verdict.results[0].proven is False
    assert "did not go red" in verdict.results[0].evidence
    assert (repo / "src" / "target.py").read_text(encoding="utf-8") == "VALUE = 2  # still uncommitted\n"
    assert sorted(_git(repo, "status", "--porcelain").splitlines()) == [" M src/target.py"]


def test_mutation_of_a_committed_fix_reverts_in_a_fresh_worktree_not_the_checkout(repo: Path) -> None:
    _commit_fix(repo)
    (repo / "src" / "other.py").write_text("OTHER = 5  # unrelated uncommitted\n", encoding="utf-8")
    claim = _claim(repo, f"mutation: {TEST_CMD} :: src/target.py @ main")
    verdict = pi.verify_claim(repo, claim.claim_id)
    result = verdict.results[0]
    assert result.proven is True, result.evidence
    assert "worktree" in result.evidence
    assert (repo / "src" / "target.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (repo / "src" / "other.py").read_text(encoding="utf-8") == "OTHER = 5  # unrelated uncommitted\n"
    assert set(_worktrees(repo)) == {str(repo)}


def test_mutation_of_a_committed_new_file_treats_absence_at_base_as_the_pre_fix_state(repo: Path) -> None:
    """A fix that ADDS a module cannot be checked out from the base; reverting it means deleting it."""
    (repo / "src" / "helper.py").write_text("HELPED = True\n", encoding="utf-8")
    (repo / "tests" / "test_helper.py").write_text(
        "import os\n\n\ndef test_helper():\n    assert os.path.exists('src/helper.py')\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: helper (new file)")
    claim = _claim(repo, f"mutation: {sys.executable} -m pytest tests/test_helper.py -q -p no:cacheprovider :: src/helper.py @ main")
    verdict = pi.verify_claim(repo, claim.claim_id)
    assert verdict.results[0].proven is True, verdict.results[0].evidence
    assert "absent" in verdict.results[0].evidence
    assert (repo / "src" / "helper.py").is_file(), "the checkout is never touched"


def test_mutation_command_that_cannot_run_is_unproven_not_red(repo: Path) -> None:
    """A failing exit is not RED unless the same command exits 0 on the unmodified tree — even when it fails at an
    assertion: an always-red `assert False` must be caught by the PRECONDITION, not by the exit-code reading (round two)."""
    _commit_fix(repo)
    always_red = f"{sys.executable} -c 'assert False, \"always red\"'"
    claim = _claim(repo, f"mutation: {always_red} :: src/target.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False
    assert "does not pass on the unmodified" in result.evidence and "exit 1" in result.evidence, result.evidence
    claim = _claim(repo, f"mutation: {sys.executable} -c 'import sys; sys.exit(9)' :: src/target.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False and "does not pass on the unmodified" in result.evidence and "exit 9" in result.evidence
    # working-directory mode has the same precondition
    (repo / "src" / "target.py").write_text("VALUE = 2  # wip\n", encoding="utf-8")
    claim = _claim(repo, f"mutation: {always_red} :: src/target.py")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False and "does not pass on the unmodified" in result.evidence, result.evidence
    assert (repo / "src" / "target.py").read_text(encoding="utf-8") == "VALUE = 2  # wip\n"


def test_mutation_precondition_is_measured_in_the_fresh_tree_not_the_checkout(repo: Path) -> None:
    """The checkout passes only because of an uncommitted edit OUTSIDE the fix paths; the tip does not. Unproven."""
    _commit_fix(repo)
    (repo / "tests" / "test_target.py").write_text(
        "def test_value():\n    assert open('src/target.py').read().strip() == 'VALUE = 3'\n", encoding="utf-8"
    )
    _git(repo, "commit", "-q", "-am", "test: wants 3 (red at the tip)")
    (repo / "tests" / "test_target.py").write_text(GOOD_TEST, encoding="utf-8")  # uncommitted: the checkout is green
    assert subprocess.run(shlex.split(TEST_CMD), cwd=repo, check=False).returncode == 0
    claim = _claim(repo, f"mutation: {TEST_CMD} :: src/target.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False
    assert "does not pass on the unmodified tree" in result.evidence, result.evidence


PYTEST_HELPER = f"{sys.executable} -m pytest tests/test_helper.py -q -p no:cacheprovider"
EXIT_1_CRASH_SHAPES = {
    "plain-script": ("import helper  # noqa: F401\n", f"{sys.executable} tests/test_helper.py"),
    "pytest-in-body-import": (
        "def test_nothing():\n    import helper  # noqa: F401\n",
        f"{sys.executable} -m pytest tests/test_helper.py -q -p no:cacheprovider",
    ),
    "unittest": (
        "import unittest\n\nimport helper  # noqa: F401\n\n\nclass T(unittest.TestCase):\n    def test_nothing(self):\n        pass\n",
        f"{sys.executable} -m unittest tests/test_helper.py",
    ),
}


@pytest.mark.parametrize("shape", sorted(EXIT_1_CRASH_SHAPES))
def test_mutation_whose_reverted_run_dies_with_exit_1_but_no_failed_assertion_is_unproven(repo: Path, shape: str) -> None:
    """Every common runner reports an uncaught exception as exit 1. Under pytest the plugin records the failure kind, so a
    test that merely imports what the fix adds is a CRASH; under any other runner the kind is NOT MEASURABLE and the
    mutation is unproven for that stated reason — never proven on the strength of exit codes or output text."""
    body, command = EXIT_1_CRASH_SHAPES[shape]
    (repo / "src" / "helper.py").write_text("HELPED = True\n", encoding="utf-8")
    (repo / "tests" / "test_helper.py").write_text(body, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: helper + a test that asserts nothing")
    claim = _claim(repo, f"mutation: {command} :: src/helper.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False, result.evidence
    assert "exit 1" in result.evidence and "assertion" in result.evidence.lower(), result.evidence
    if shape == "pytest-in-body-import":
        assert "crashed" in result.evidence and "ModuleNotFoundError" in result.evidence, result.evidence
    else:
        assert "not measurable outside pytest" in result.evidence, result.evidence


SPOOF_LINES = [  # round three: output text the test itself prints must never count as the runner's verdict
    "AssertionError",
    "  Failed: 0",
    "FAILED tests/test_helper.py::test_nothing - assert False",
    "E       assert False",
    "FAIL: test_nothing (tests.test_helper.T.test_nothing)",
]


@pytest.mark.parametrize("line", SPOOF_LINES)
def test_mutation_is_not_proven_by_a_test_that_prints_the_runner_verdict_and_then_crashes(repo: Path, line: str) -> None:
    (repo / "src" / "helper.py").write_text("HELPED = True\n", encoding="utf-8")
    (repo / "tests" / "test_helper.py").write_text(
        f"def test_nothing():\n    print({line!r})\n    import helper  # noqa: F401\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: helper + a test that prints a verdict")
    claim = _claim(repo, f"mutation: {PYTEST_HELPER} :: src/helper.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False and "crashed" in result.evidence, result.evidence


def test_mutation_in_the_working_directory_is_not_proven_by_a_test_that_prints_a_verdict(repo: Path) -> None:
    """The in-place path reads the plugin's record too: an uncommitted fix reverted in place, a test that prints
    'AssertionError' and then crashes on the missing module, is a crash."""
    (repo / "src" / "helper.py").write_text("HELPED = True\n", encoding="utf-8")  # uncommitted: absent at HEAD
    (repo / "tests" / "test_helper.py").write_text(
        "def test_nothing():\n    print('AssertionError')\n    import helper  # noqa: F401\n", encoding="utf-8"
    )
    claim = _claim(repo, f"mutation: {PYTEST_HELPER} :: src/helper.py")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False and "crashed" in result.evidence and "ModuleNotFoundError" in result.evidence, result.evidence
    assert (repo / "src" / "helper.py").read_text(encoding="utf-8") == "HELPED = True\n", "restored"


def test_mutation_whose_only_assertion_is_in_a_fixture_is_a_setup_error_not_red(repo: Path) -> None:
    """An assertion inside a fixture is a SETUP error: the test's own call phase never ran, so pytest records nothing
    and the mutation is a crash. (The plugin deliberately records call-phase failures only.)"""
    (repo / "src" / "helper.py").write_text("HELPED = True\n", encoding="utf-8")
    (repo / "tests" / "test_helper.py").write_text(
        "import importlib.util\n\nimport pytest\n\n\n@pytest.fixture\ndef helper_present():\n"
        "    assert importlib.util.find_spec('helper') is not None\n\n\ndef test_nothing(helper_present):\n    pass\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: helper + a fixture that asserts")
    claim = _claim(repo, f"mutation: {PYTEST_HELPER} :: src/helper.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False and "crashed" in result.evidence and "no test reached its call phase" in result.evidence, result.evidence


def test_mutation_under_a_shell_wrapper_that_echoes_a_verdict_is_not_measurable(repo: Path) -> None:
    (repo / "src" / "helper.py").write_text("HELPED = True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: helper")
    command = "sh -c 'test -f src/helper.py || { echo AssertionError; exit 1; }'"
    claim = _claim(repo, f"mutation: {command} :: src/helper.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False and "not measurable outside pytest" in result.evidence, result.evidence


def test_mutation_whose_test_asserts_the_fix_exists_is_proven(repo: Path) -> None:
    """The honest shape for a fix that adds a module: assert its presence, so the base run fails at an assertion."""
    (repo / "src" / "helper.py").write_text("HELPED = True\n", encoding="utf-8")
    (repo / "tests" / "test_helper.py").write_text(
        "import importlib.util\n\n\ndef test_helper_exists():\n    assert importlib.util.find_spec('helper') is not None\n"
        "    import helper\n\n    assert helper.HELPED\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: helper + a test that asserts it exists")
    command = f"{sys.executable} -m pytest tests/test_helper.py -q -p no:cacheprovider"
    claim = _claim(repo, f"mutation: {command} :: src/helper.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is True, result.evidence


def test_mutation_runs_never_write_bytecode_so_a_same_size_revert_cannot_reuse_the_fix(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Python trusts a .pyc by source mtime (1 s) + size; a same-size revert in the same second would run the FIX's
    bytecode and read 'did not go red' (round two, nondeterministic). Every run prove-it launches refuses to write bytecode."""
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)  # an inherited setting must not pass this for the code
    (repo / "src" / "target.py").write_text("VALUE = 2\n", encoding="utf-8")  # same size as the base's VALUE = 1
    (repo / "tests" / "test_flag.py").write_text(
        "import sys\n\n\ndef test_flag():\n    assert sys.dont_write_bytecode\n    import target\n\n    assert target.VALUE == 2\n",
        encoding="utf-8",
    )
    command = f"{sys.executable} -m pytest tests/test_flag.py -q -p no:cacheprovider"
    claim = _claim(repo, f"mutation: {command} :: src/target.py")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is True, result.evidence
    assert not (repo / "src" / "__pycache__").exists() and not (repo / "tests" / "__pycache__").exists()


def test_mutation_refuses_a_symlink_path_rather_than_writing_through_it(repo: Path) -> None:
    """HEAD's blob for a symlink is the link TARGET string; writing it through the link corrupts the file it points at,
    and that corruption is what would make the test 'go red' (round two)."""
    (repo / "src" / "real.py").write_text("REAL = 1\n", encoding="utf-8")
    os.symlink("real.py", repo / "src" / "link.py")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "link.py -> real.py")
    (repo / "src" / "link.py").unlink()
    os.symlink("other.py", repo / "src" / "link.py")  # uncommitted: now points at other.py
    (repo / "tests" / "test_link.py").write_text(
        "def test_other():\n    assert open('src/other.py').read() == 'OTHER = 1\\n'\n", encoding="utf-8"
    )
    command = f"{sys.executable} -m pytest tests/test_link.py -q -p no:cacheprovider"
    claim = _claim(repo, f"mutation: {command} :: src/link.py")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False and "symlink" in result.evidence, result.evidence
    assert (repo / "src" / "other.py").read_text(encoding="utf-8") == "OTHER = 1\n"
    assert os.readlink(repo / "src" / "link.py") == "other.py"


def test_mutation_that_crashes_instead_of_failing_is_unproven(repo: Path) -> None:
    """RED means pytest itself recorded a FAILED ASSERTION. A collection error (exit 2) and an uncaught RuntimeError
    inside the test (exit 1, recorded by the plugin as RuntimeError) are both crashes, never RED."""
    (repo / "tests" / "test_target.py").write_text(
        "def test_value():\n    if open('src/target.py').read().strip() != 'VALUE = 2':\n        raise RuntimeError('boom')\n",
        encoding="utf-8",
    )
    _git(repo, "commit", "-q", "-am", "test: raises when unhappy")
    _commit_fix(repo)
    claim = _claim(repo, f"mutation: {TEST_CMD} :: src/target.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False
    assert "crashed" in result.evidence and "exit 1" in result.evidence and "RuntimeError" in result.evidence, result.evidence
    (repo / "tests" / "test_target.py").write_text(
        "import missing_module  # noqa: F401\n\n\ndef test_value():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "commit", "-q", "-am", "test: cannot even be collected")
    claim = _claim(repo, f"mutation: {TEST_CMD} :: src/target.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False and "does not pass on the unmodified" in result.evidence and "exit 2" in result.evidence, result.evidence


def test_committed_mutation_without_a_base_is_unproven_not_guessed(repo: Path) -> None:
    _commit_fix(repo)
    claim = _claim(repo, f"mutation: {TEST_CMD} :: src/target.py")
    verdict = pi.verify_claim(repo, claim.claim_id)
    assert verdict.results[0].proven is False
    assert "base" in verdict.results[0].evidence


# ----------------------------------------------------------------- verdict files + review


def test_verdict_file_names_every_unproven_assertion_and_review_finalises(repo: Path) -> None:
    sha = _commit_fix(repo)
    claim = _claim(repo, f"test: {TEST_CMD}", f"commit: {sha} on work", "file: missing.txt")
    verdict = pi.verify_claim(repo, claim.claim_id)
    assert verdict.verdict == pi.VERDICT_FAIL and verdict.unproven == ["A3"]
    files = _verdicts(repo)
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert text.startswith("# prove-it verdict — FAIL")
    assert f"session: {SESSION}" in text and f"claim: {claim.claim_id}" in text and "verdict: FAIL" in text
    assert "A3" in text and "missing.txt" in text
    assert "UNPROVEN" in text and "A1" in text and "PROVEN" in text

    good = _claim(repo, f"test: {TEST_CMD}", f"commit: {sha} on work")
    pending = pi.verify_claim(repo, good.claim_id)
    assert pending.verdict == pi.VERDICT_UNREVIEWED
    assert "review: none" in _verdicts(repo)[-1].read_text(encoding="utf-8")
    failed_review = pi.record_review(repo, good.claim_id, verdict="FAIL", findings="A1 passes for the wrong reason")
    assert failed_review.verdict == pi.VERDICT_FAIL
    assert "A1 passes for the wrong reason" in _verdicts(repo)[-1].read_text(encoding="utf-8")
    final = pi.record_review(repo, good.claim_id, verdict="PASS", findings="could not falsify any assertion")
    assert final.verdict == pi.VERDICT_PASS
    assert _verdicts(repo)[-1].read_text(encoding="utf-8").startswith("# prove-it verdict — PASS")
    assert pi.latest_verdict(repo, session_id=SESSION).verdict == pi.VERDICT_PASS
    assert pi.latest_verdict(repo, session_id=OTHER_SESSION) is None


def test_review_cannot_pass_a_deterministic_failure(repo: Path) -> None:
    claim = _claim(repo, "file: missing.txt")
    assert pi.verify_claim(repo, claim.claim_id).verdict == pi.VERDICT_FAIL
    assert pi.record_review(repo, claim.claim_id, verdict="PASS", findings="looks fine to me").verdict == pi.VERDICT_FAIL


def test_withdraw_records_the_reason(repo: Path) -> None:
    claim = _claim(repo, "file: missing.txt")
    withdrawn = pi.withdraw_claim(repo, claim.claim_id, reason="could not substantiate A1; reporting as incomplete")
    assert withdrawn.withdrawn is not None and "incomplete" in withdrawn.withdrawn["reason"]
    raw = json.loads((repo / ".verify" / "claims" / f"{claim.claim_id}.json").read_text(encoding="utf-8"))
    assert raw["withdrawn"]["reason"].startswith("could not substantiate")


def test_withdraw_without_a_claim_records_a_session_withdrawal(repo: Path) -> None:
    """A session that did work and wants to report failure must be able to withdraw without inventing a claim."""
    record = pi.withdraw_session(repo, session_id=SESSION, reason="could not reproduce the bug; nothing to claim")
    assert record.withdrawn is not None and record.assertions == []
    assert [c.claim_id for c in pi.claims_for_session(repo, SESSION)] == [record.claim_id]


def test_cli_round_trip(repo: Path, tmp_path: Path) -> None:
    sha = _commit_fix(repo)
    base = [sys.executable, str(SCRIPT), "--repo", str(repo)]

    def run(*a: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*base, *a], capture_output=True, text=True, check=False, env={**GIT_ENV, "CLAUDE_CODE_SESSION_ID": SESSION})

    created = run("claim", "--claim", "fixed it", "--assert", f"test: {TEST_CMD}", "--assert", f"commit: {sha} on work")
    assert created.returncode == 0, created.stderr
    claim_id = json.loads(created.stdout)["claim_id"]
    verified = run("verify", "--claim", claim_id)
    assert verified.returncode == 1, "unreviewed is not a pass"
    assert json.loads(verified.stdout)["verdict"] == pi.VERDICT_UNREVIEWED
    findings = tmp_path / "review.md"
    findings.write_text("Tried to falsify A1 and A2; could not.\n", encoding="utf-8")
    reviewed = run("review", "--claim", claim_id, "--verdict", "PASS", "--findings", str(findings))
    assert reviewed.returncode == 0 and json.loads(reviewed.stdout)["verdict"] == pi.VERDICT_PASS
    status = run("status")
    assert status.returncode == 0 and json.loads(status.stdout)["verdict"] == pi.VERDICT_PASS
    withdrawn = run("withdraw", "--claim", claim_id, "--reason", "changed my mind")
    assert withdrawn.returncode == 0
    claimless = run("withdraw", "--reason", "not claiming the rest")  # the form the hook names for a session with no claim
    assert claimless.returncode == 0, claimless.stderr
    assert json.loads(claimless.stdout)["withdrawn"]["reason"] == "not claiming the rest"


# ----------------------------------------------------------------- the Stop hook


def _iso(offset_seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _transcript(
    path: Path, events: list[tuple[str, str, dict[str, object]]], *, final: str = "Done — the work is complete and tests pass."
) -> None:
    """events: (iso timestamp, tool name, tool input) — the shape Claude Code writes per assistant tool_use.

    ``final`` is the assistant's last text message: the Stop hook only enforces when it reads as a completion claim.
    """
    lines = []
    for event in events:
        when, tool, tool_input = event[:3]
        tool_use_id = event[3] if len(event) > 3 else "x"  # type: ignore[misc]
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": when,
                    "sessionId": SESSION,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": tool_use_id, "name": tool, "input": tool_input}],
                    },
                }
            )
        )
    lines.append(json.dumps({"type": "user", "timestamp": _iso(0), "message": {"role": "user", "content": "hi"}}))
    lines.append(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": _iso(1),
                "sessionId": SESSION,
                "message": {"role": "assistant", "content": [{"type": "text", "text": final}]},
            }
        )
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _tool_result(transcript: Path, tool_use_id: str, when: str, text: str = "done") -> None:
    """Append what Claude Code writes when a foreground subagent hands back: a user-role tool_result for the Agent call."""
    content = [{"type": "tool_result", "tool_use_id": tool_use_id, "content": [{"type": "text", "text": text}]}]
    line = json.dumps({"type": "user", "timestamp": when, "message": {"role": "user", "content": content}})
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _hook(repo: Path, transcript: Path, *, session: str = SESSION, active: bool = False) -> tuple[int, dict[str, object], str]:
    payload = {
        "session_id": session,
        "transcript_path": str(transcript),
        "cwd": str(repo),
        "hook_event_name": "Stop",
        "stop_hook_active": active,
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, check=False, cwd=repo, env=GIT_ENV
    )
    out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    return proc.returncode, out, proc.stderr


def test_hook_allows_a_session_that_did_no_work(repo: Path, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-30), "Read", {"file_path": "x"}), (_iso(-20), "Bash", {"command": "git status"})])
    code, out, _ = _hook(repo, transcript)
    assert code == 0 and out.get("decision") != "block"


def test_hook_gates_on_work_not_on_wording(repo: Path, tmp_path: Path) -> None:
    """The hook reads no prose (John, 2026-09-09: 'the prose classifier is not a good idea'). Unreleased work blocks
    whatever the final message says; only a PASS or a withdrawal releases it."""
    transcript = tmp_path / "t.jsonl"
    for text in (
        "Gate still running at 85%; waiting for the marker before I do anything else.",
        "The work is NOT done and the test still fails.",
        "Here is how it works: the resolver reads the manifest.",
        "",
    ):
        _transcript(transcript, [(_iso(-30), "Edit", {"file_path": "src/target.py"})], final=text)
        shutil.rmtree(repo / ".verify" / ".hook", ignore_errors=True)
        assert _hook(repo, transcript)[1].get("decision") == "block", text


def test_hook_blocks_work_with_no_claim_and_names_what_to_do(repo: Path, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-30), "Edit", {"file_path": str(repo / "src" / "target.py")})])
    code, out, _ = _hook(repo, transcript)
    assert code == 0 and out["decision"] == "block"
    assert "prove_it.py claim" in out["reason"] and "no claim" in out["reason"].lower()


def test_hook_counts_bash_writes_and_commits_as_work(repo: Path, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-30), "Bash", {"command": "cat > src/target.py <<'EOF'\nVALUE = 2\nEOF"})])
    assert _hook(repo, transcript)[1]["decision"] == "block"
    _transcript(transcript, [(_iso(-30), "Bash", {"command": "git commit -am 'fix'"})])
    assert _hook(repo, transcript)[1]["decision"] == "block"
    _transcript(transcript, [(_iso(-30), "Bash", {"command": "python .agents/skills/prove-it/prove_it.py verify --claim x"})])
    assert _hook(repo, transcript)[1].get("decision") != "block", "the verifier's own commands are not work"


def test_hook_blocks_until_a_pass_verdict_newer_than_the_last_work(repo: Path, tmp_path: Path) -> None:
    sha = _commit_fix(repo)
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-60), "Edit", {"file_path": "src/target.py"})])
    claim = _claim(repo, f"test: {TEST_CMD}", f"commit: {sha} on work")
    code, out, _ = _hook(repo, transcript)
    assert out["decision"] == "block" and "has not been verified" in out["reason"], out["reason"]
    pi.verify_claim(repo, claim.claim_id)  # UNREVIEWED
    code, out, _ = _hook(repo, transcript)
    assert out["decision"] == "block" and "adversarial review is not recorded" in out["reason"], out["reason"]
    pi.record_review(repo, claim.claim_id, verdict="FAIL", findings="A1 flaky")
    code, out, _ = _hook(repo, transcript)
    assert out["decision"] == "block" and "verdict FAIL" in out["reason"] and "withdraw" in out["reason"], out["reason"]
    pi.record_review(repo, claim.claim_id, verdict="PASS", findings="could not falsify")
    code, out, _ = _hook(repo, transcript)
    assert code == 0 and out.get("decision") != "block"
    # new work after the PASS re-arms the gate
    _transcript(transcript, [(_iso(-60), "Edit", {"file_path": "src/target.py"}), (_iso(30), "Write", {"file_path": "src/new.py"})])
    out = _hook(repo, transcript)[1]
    assert out["decision"] == "block" and "has no claim covering it" in out["reason"], out["reason"]


def test_hook_releases_a_withdrawn_claim_and_a_claim_less_withdrawal(repo: Path, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-60), "Edit", {"file_path": "src/target.py"})])
    claim = _claim(repo, "file: missing.txt")
    pi.verify_claim(repo, claim.claim_id)
    assert _hook(repo, transcript)[1]["decision"] == "block"
    pi.withdraw_claim(repo, claim.claim_id, reason="cannot substantiate; reporting incomplete")
    code, out, _ = _hook(repo, transcript)
    assert code == 0 and out.get("decision") != "block"
    assert "withdrawn" in out.get("systemMessage", "").lower()
    # a claim-less withdrawal releases too
    time.sleep(1.1)
    _transcript(transcript, [(_iso(-30), "Edit", {"file_path": "src/target.py"}), (_iso(0), "Write", {"file_path": "src/new.py"})])
    assert _hook(repo, transcript)[1]["decision"] == "block"
    time.sleep(1.1)
    pi.withdraw_session(repo, session_id=SESSION, reason="new.py is a stub; not claiming it")
    code, out, _ = _hook(repo, transcript)
    assert code == 0 and out.get("decision") != "block" and "withdrawn" in out.get("systemMessage", "").lower()


def test_hook_ignores_other_sessions_claims_in_both_directions(repo: Path, tmp_path: Path) -> None:
    """A foreign PASS must not release this session; a foreign FAIL newer than this session's PASS must not block it.
    Both halves fail if the session filter is dropped (round two: the earlier version of this test was vacuous)."""
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-30), "Edit", {"file_path": "src/target.py"})])
    sha = _commit_fix(repo)
    foreign_pass = _claim(repo, f"commit: {sha} on work", session=OTHER_SESSION)
    pi.verify_claim(repo, foreign_pass.claim_id)
    pi.record_review(repo, foreign_pass.claim_id, verdict="PASS", findings="ok")
    assert pi.latest_verdict(repo, session_id=OTHER_SESSION).verdict == pi.VERDICT_PASS
    assert _hook(repo, transcript)[1].get("decision") == "block", "another session's PASS is not this session's"
    mine = _claim(repo, f"commit: {sha} on work")
    pi.verify_claim(repo, mine.claim_id)
    pi.record_review(repo, mine.claim_id, verdict="PASS", findings="ok")
    time.sleep(1.1)
    foreign_fail = _claim(repo, "file: missing.txt", session=OTHER_SESSION)
    pi.verify_claim(repo, foreign_fail.claim_id)
    assert pi.latest_verdict(repo, session_id=OTHER_SESSION).verdict == pi.VERDICT_FAIL
    code, out, _ = _hook(repo, transcript)
    assert code == 0 and out.get("decision") != "block", "a newer FAIL from another session does not block this one"


def test_hook_block_reason_names_the_claim_less_withdrawal(repo: Path, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-30), "Edit", {"file_path": "src/target.py"})])
    reason = str(_hook(repo, transcript)[1]["reason"])
    assert "withdraw --reason" in reason and "--claim" in reason, reason


WORK_COMMANDS = {  # every channel that changes files or history (round two: the list was Edit/Write + git commit + a few)
    "cat-heredoc": "cat > src/target.py <<'EOF'\nVALUE = 2\nEOF",
    "git-commit": "git commit -am 'fix'",
    "cp": "cp scratch/fixed.py src/target.py",
    "mv": "mv src/old.py src/new.py",
    "rm": "rm -rf build/",
    "redirect-txt": "echo notes > notes.txt",
    "heredoc-dockerfile": "cat <<'EOF' > Dockerfile\nFROM python\nEOF",
    "python-c-write": "python -c \"open('src/target.py','w').write('VALUE = 2')\"",
    "python-write-text": "python - <<'EOF'\nfrom pathlib import Path\nPath('x.py').write_text('1')\nEOF",
    "git-push": "git push origin HEAD",
    "gh-pr-merge": "gh pr merge 42 --squash",
    "git-reset-hard": "git reset --hard && git clean -fd",
    "sed-i": "sed -i 's/a/b/' src/target.py",
    "tee": "printf x | tee src/target.py",
    "lane-manager-merge": "python .agents/skills/lane-manager/lane_manager.py merge --lane x",
    # round three: channels this repo's workflow uses
    "ruff-format": "ruff format src/",
    "ruff-fix": "ruff check --fix src/",
    "black": "black src/",
    "pre-commit": "pre-commit run --all-files",
    "canonical-execute": "./scripts/worktree-cleanup.sh --execute",
    "git-pull": "git pull --rebase",
    "git-branch-f": "git branch -f lane/x HEAD",
    "git-C": "git -C other/repo commit -m x",
    "git-dir": "git --git-dir=.git commit -m x",
    "gh-api-delete": "gh api -X DELETE repos/x/y",
    "self-then-rm": "python .agents/skills/prove-it/prove_it.py status; rm -rf src/",
    "sed-i-on-hook": "sed -i 's/x/y/' .agents/skills/prove-it/stop_hook.py",
    "redirect-1": "echo x 1> src/generated.py",
    "redirect-2": "make 2> src/errors.txt",
    "in-repo-log": "pytest tests/ > tests/fixtures/expected.log",
    "uv-pip-install": "uv pip install foo",
    "find-delete": "find build -name '*.pyc' -delete",
    "xargs-rm": "ls | xargs rm",
    # round four: one pin per alternative
    "perl-i": "perl -pi -e 's/a/b/' src/x.py",
    "mkdir": "mkdir -p src/newpkg",
    "touch": "touch src/newpkg/__init__.py",
    "chmod": "chmod +x scripts/x.sh",
    "ln": "ln -s ../real.py src/link.py",
    "chown": "chown john: data/",
    "install": "install -m 755 x.sh scripts/",
    "patch": "patch -p1 < fix.diff",
    "rsync": "rsync -a build/ dist/",
    "truncate": "truncate -s 0 data/x.db",
    "shred": "shred -u secrets.txt",
    "dd": "dd if=/dev/zero of=data/blob bs=1M count=1",
    "sponge": "sort x.txt | sponge x.txt",
    "unzip": "unzip release.zip -d vendor/",
    "ed": "ed -s src/x.py < script.ed",
    "ex": "ex -c '%s/a/b/g' -c wq src/x.py",
    "rmdir": "rmdir build/empty",
    "tar-x": "tar xzf vendor.tgz -C vendor/",
    "isort": "isort src/",
    "autopep8": "autopep8 -i src/x.py",
    "prettier": "prettier --write web/src/",
    "eslint-fix": "eslint --fix web/src/",
    "npm-install": "npm install",
    "uv-sync": "uv sync",
    "uv-lock": "uv lock",
    "pip-install": "pip install requests",
    "alembic": "alembic upgrade head",
    "sqlite-delete": "sqlite3 data/x.db 'DELETE FROM runs'",
    "git-worktree-add": "git worktree add ../wt lane/x",
    "git-update-ref": "git update-ref refs/heads/x HEAD",
    "git-update-index": "git update-index --assume-unchanged x",
    "git-symbolic-ref": "git symbolic-ref HEAD refs/heads/x",
    "git-notes": "git notes add -m x HEAD",
    "git-gc": "git gc --prune=now",
    "git-reflog-expire": "git reflog expire --expire=now --all",
    "git-filter-branch": "git filter-branch --tree-filter 'rm x' HEAD",
    "git-alias": "git -c alias.ci=commit ci -m x",
    "gh-pr-close": "gh pr close 42",
    "gh-pr-edit": "gh pr edit 42 --title x",
    "gh-issue-delete": "gh issue delete 7",
    "gh-pr-comment": "gh pr comment 42 --body x",
    "gh-pr-reopen": "gh pr reopen 42",
    "gh-pr-ready": "gh pr ready 42",
    "python-open-w": "python -c \"f = open('x.txt', 'w'); f.close()\"",
    "python-subprocess-list": "python -c \"import subprocess; subprocess.run(['git', 'commit', '-am', 'x'])\"",
    "filigree-close": "filigree close elspeth-abc123 --reason done",
    "filigree-start-next": "filigree start-next-work --assignee lane-3",
    "filigree-update": "filigree update elspeth-abc123 --status closed",
    "filigree-comment": "filigree add-comment elspeth-abc123 --body x",
    "elspeth-run-execute": "elspeth run --settings examples/x/settings.yaml --execute",
    "redirect-traversal": "echo x > /tmp/../home/john/elspeth/src/x.py",
    "redirect-scratchpad-file": "echo x > src/scratchpad_utils.py",
}
NOT_WORK_COMMANDS = {
    "canonical-dry-run": "./scripts/worktree-cleanup.sh --path 'lane-*'",
    "ruff-check": "ruff check src/",
    "git-fetch": "git fetch origin",
    "self-status": "python .agents/skills/prove-it/prove_it.py status",
    "filigree-list": "filigree list --status open",
    "filigree-show": "filigree show elspeth-abc123",
    "filigree-session-context": "filigree session-context",
    "elspeth-run-dry": "elspeth run --settings examples/x/settings.yaml",
    "ls": "ls -la src/",
    "cat": "cat src/target.py",
    "grep": "grep -rn VALUE src/",
    "git-status": "git status --short && git diff --stat",
    "git-log": "git log --oneline -5",
    "pytest-plain": "pytest tests/unit/test_x.py -n 0 -q",
    "pytest-to-log-variable": 'pytest tests/ > "$log" 2>&1; echo exit=$?',
    "redirect-devnull": "make lint > /dev/null 2>&1",
    "redirect-tmp": "pytest tests/ > /tmp/run.log 2>&1",
    "redirect-scratchpad": "pytest tests/ > /home/x/.cache/scratchpad/run.log 2>&1",
    "python-c-print": "python -c 'print(1+1)'",
    "verifier": "python .agents/skills/prove-it/prove_it.py verify --claim x",
}


@pytest.mark.parametrize("name", sorted(WORK_COMMANDS))
def test_hook_counts_every_write_channel_as_work(repo: Path, tmp_path: Path, name: str) -> None:
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-30), "Bash", {"command": WORK_COMMANDS[name]})])
    assert _hook(repo, transcript)[1].get("decision") == "block", name


@pytest.mark.parametrize("name", sorted(NOT_WORK_COMMANDS))
def test_hook_does_not_count_reads_tests_and_scratch_logs_as_work(repo: Path, tmp_path: Path, name: str) -> None:
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-30), "Bash", {"command": NOT_WORK_COMMANDS[name]})])
    assert _hook(repo, transcript)[1].get("decision") != "block", name


MCP_WORK = [
    "mcp__elspeth-judge__stage_annotate",
    "mcp__filigree__work_finish",
    "mcp__filigree__issue_update",
    "mcp__elspeth-composer__set_pipeline",
    "mcp__filigree__resolve_annotation",  # a writer whose first token is a read verb elsewhere
]
MCP_READ = [
    "mcp__elspeth-judge__stage_status",
    "mcp__loomweave__entity_callers_list",
    "mcp__elspeth-composer__get_pipeline_state",
    "mcp__filigree__issue_validate",
    "mcp__loomweave__entity_resolve",
]


def test_hook_counts_mcp_writers_workflows_and_delegated_agents_as_work(repo: Path, tmp_path: Path) -> None:
    """MCP tools are work unless their verb is in a small READ allowlist (a list of writers was incomplete three rounds
    running). A parent that fans work out records only an Agent tool use; the edits live in <session>/subagents/."""
    transcript = tmp_path / "t.jsonl"
    for name in [*MCP_WORK, "Workflow", "NotebookEdit", "MultiEdit"]:
        _transcript(transcript, [(_iso(-30), name, {"x": "y"})])
        shutil.rmtree(repo / ".verify" / ".hook", ignore_errors=True)
        assert _hook(repo, transcript)[1].get("decision") == "block", name
    _transcript(transcript, [(_iso(-30 + i), name, {"q": "x"}) for i, name in enumerate([*MCP_READ, "Read", "Grep"])])
    assert _hook(repo, transcript)[1].get("decision") != "block"
    _transcript(transcript, [(_iso(-30), "Agent", {"prompt": "look around", "subagent_type": "Explore"})])
    assert _hook(repo, transcript)[1].get("decision") != "block", "an Agent use alone is not work: Explore agents edit nothing"
    sub = tmp_path / "t" / "subagents" / "agent-abc123.jsonl"
    sub.parent.mkdir(parents=True)
    (sub.parent / "agent-abc123.meta.json").write_text(json.dumps({"name": "worker-1", "agentType": "worker-1"}), encoding="utf-8")
    _transcript(sub, [(_iso(-20), "Edit", {"file_path": "src/target.py"})], final="Edited src/target.py as asked.")
    assert _hook(repo, transcript)[1].get("decision") != "block", (
        "a subagent still running has handed nothing back: not yet this session's work"
    )
    _idle_notification(transcript, "worker-1", _iso(-10))
    assert _hook(repo, transcript)[1].get("decision") == "block", "once the subagent reports idle, its Edit is this session's work"
    time.sleep(1.1)
    pi.withdraw_session(repo, session_id=SESSION, reason="worker-1's edit is a stub")
    assert _hook(repo, transcript)[1].get("decision") != "block"
    # woken again and editing after its last idle notification: not counted until it reports idle again
    _transcript(sub, [(_iso(-20), "Edit", {"file_path": "src/target.py"}), (_iso(30), "Edit", {"file_path": "src/later.py"})], final="more")
    assert _hook(repo, transcript)[1].get("decision") != "block"
    _idle_notification(transcript, "worker-1 [3fa9c1]", _iso(31))  # real notifications carry a display suffix
    assert _hook(repo, transcript)[1].get("decision") == "block"


def _idle_notification(transcript: Path, name: str, when: str) -> None:
    """Append what Claude Code writes when a background subagent reports idle: a user-role teammate message."""
    body = json.dumps({"type": "idle_notification", "from": name, "timestamp": when, "idleReason": "available", "result": "done"})
    text = f'<teammate-message teammate_id="{name}">\n{body}\n</teammate-message>'
    line = json.dumps({"type": "user", "timestamp": when, "message": {"role": "user", "content": text}})
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def test_hook_caps_repeated_blocks_so_a_session_cannot_loop_forever(repo: Path, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-60), "Edit", {"file_path": "src/target.py"})])
    decisions = [_hook(repo, transcript, active=True)[1] for _ in range(pi.MAX_BLOCKS + 1)]
    assert all(d["decision"] == "block" for d in decisions[: pi.MAX_BLOCKS])
    assert decisions[-1].get("decision") != "block"
    assert "NOT verified" in decisions[-1]["systemMessage"]
    # the valve is per SESSION until a release: a fresh work signal must not re-arm five more blocks
    _transcript(transcript, [(_iso(-60), "Edit", {"file_path": "src/target.py"}), (_iso(0), "Bash", {"command": "pytest | tee run.log"})])
    again = _hook(repo, transcript, active=True)[1]
    assert again.get("decision") != "block" and "NOT verified" in again["systemMessage"]
    # a withdrawal releases and resets the valve, so later work is gated again
    time.sleep(1.1)
    pi.withdraw_session(repo, session_id=SESSION, reason="giving up on this one")
    assert _hook(repo, transcript)[1].get("decision") != "block"
    _transcript(transcript, [(_iso(-60), "Edit", {"file_path": "src/target.py"}), (_iso(30), "Edit", {"file_path": "src/later.py"})])
    assert _hook(repo, transcript)[1]["decision"] == "block"


def test_hook_never_crashes_on_a_missing_transcript(repo: Path, tmp_path: Path) -> None:
    code, out, _err = _hook(repo, tmp_path / "does-not-exist.jsonl")
    assert code == 0 and out.get("decision") != "block"


# ----------------------------------------------------------------- installation + skill doc


def test_stop_hook_is_installed_in_project_settings() -> None:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    commands = [h["command"] for entry in settings["hooks"]["Stop"] for h in entry["hooks"] if h["type"] == "command"]
    assert any("prove-it/stop_hook.py" in c and "CLAUDE_PROJECT_DIR" in c for c in commands), commands
    assert (REPO_ROOT / ".claude" / "skills" / "prove-it").is_symlink() or (REPO_ROOT / ".claude" / "skills" / "prove-it").is_dir()


def test_skill_doc_names_every_step_and_the_refusal_rule() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: prove-it\n")
    for token in (
        "falsifiable",
        "fresh",
        "worktree",
        "working directory",
        "mutation",
        "snapshot",
        "never",
        "git rev-parse",
        ".verify/",
        "PASS",
        "FAIL",
        "UNREVIEWED",
        "withdraw",
        "adversarial",
        "review",
        "Stop hook",
        "could not be substantiated",
        "AssertionError",
        "find_spec",
        "subagents",
        "prove_it_red_plugin",
        "not measurable",
        "test:",
        "commit:",
        "mutation:",
        "exit0:",
        "nonzero:",
        "file:",
    ):
        assert token in text, token


# ----------------------------------------------------------------- round five


def test_mutation_ignores_xfail_records_and_asserts_raised_outside_the_test_files(repo: Path) -> None:
    """RED is pytest's OUTCOME, not an exception type seen in flight: an xfail sibling (marked or imperative) is
    reported xfailed, and an `assert` that fires inside production code is not the test's assertion (round four)."""
    (repo / "src" / "helper.py").write_text("HELPED = True\n", encoding="utf-8")
    (repo / "src" / "lib.py").write_text(
        "import importlib.util\n\n\ndef load():\n    assert importlib.util.find_spec('helper') is not None\n", encoding="utf-8"
    )
    shapes = {
        "marked": "import pytest\n\n\ndef test_v():\n    import helper  # noqa: F401\n\n\n@pytest.mark.xfail\ndef test_known_bad():\n    assert 1 == 2\n",
        "imperative": "import importlib.util\n\nimport pytest\n\n\ndef test_v():\n    import helper  # noqa: F401\n\n\ndef test_optional():\n"
        "    if importlib.util.find_spec('helper') is None:\n        pytest.xfail('optional')\n",
        "production-assert": "import lib\n\n\ndef test_v():\n    lib.load()\n",
    }
    for name, body in shapes.items():
        (repo / "tests" / "test_helper.py").write_text(body, encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", f"shape {name}")
        claim = _claim(repo, f"mutation: {PYTEST_HELPER} :: src/helper.py @ main")
        result = pi.verify_claim(repo, claim.claim_id).results[0]
        assert result.proven is False and "crashed" in result.evidence, (name, result.evidence)


def test_mutation_accepts_pytest_fail_as_the_tests_assertion_and_names_every_failing_test(repo: Path) -> None:
    """`pytest.fail` is pytest's own assertion outcome (round-four R4-1); two failing tests are both recorded (R4-2)."""
    (repo / "src" / "helper.py").write_text("HELPED = True\n", encoding="utf-8")
    (repo / "tests" / "test_helper.py").write_text(
        "import importlib.util\n\nimport pytest\n\n\ndef test_a():\n    if importlib.util.find_spec('helper') is None:\n"
        "        pytest.fail('helper missing')\n\n\ndef test_b():\n    assert importlib.util.find_spec('helper') is not None\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: helper + fail/assert tests")
    claim = _claim(repo, f"mutation: {PYTEST_HELPER} :: src/helper.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is True, result.evidence
    assert "test_a" in result.evidence and "test_b" in result.evidence, result.evidence


def test_mutation_counts_an_assertion_raised_in_a_conftest_helper_as_the_tests_own(repo: Path) -> None:
    """Test-side helpers live in conftest.py and test_*.py modules: an assert raised there is the test's assertion."""
    (repo / "src" / "helper.py").write_text("HELPED = True\n", encoding="utf-8")
    (repo / "tests" / "conftest.py").write_text(
        "import importlib.util\n\n\ndef expect_helper():\n    assert importlib.util.find_spec('helper') is not None\n", encoding="utf-8"
    )
    (repo / "tests" / "test_helper.py").write_text(
        "from conftest import expect_helper\n\n\ndef test_v():\n    expect_helper()\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: helper + conftest helper")
    claim = _claim(repo, f"mutation: {PYTEST_HELPER} :: src/helper.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is True, result.evidence


def test_mutation_is_unproven_when_the_restore_is_not_byte_identical(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "src" / "target.py").write_text("VALUE = 2\n", encoding="utf-8")
    monkeypatch.setattr(pi, "_restore_in_place", lambda repo, snapshot: False)
    claim = _claim(repo, f"mutation: {TEST_CMD} :: src/target.py")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False and "WITH DIFFERENCES" in result.evidence, result.evidence


def test_in_place_mutation_records_digests_and_review_refuses_after_the_measured_bytes_change(repo: Path) -> None:
    """A verdict binds to the bytes it measured: editing a reverted path after verify voids the review (round four)."""
    (repo / "src" / "target.py").write_text("VALUE = 2\n", encoding="utf-8")
    claim = _claim(repo, f"mutation: {TEST_CMD} :: src/target.py")
    verdict = pi.verify_claim(repo, claim.claim_id)
    result = verdict.results[0]
    assert result.proven is True
    assert result.digests == {"src/target.py": pi.sha256_of(repo / "src" / "target.py")}
    pi.record_review(repo, claim.claim_id, verdict="PASS", findings="ok")  # unchanged bytes: accepted
    (repo / "src" / "target.py").write_text("VALUE = 2  # edited after the verdict\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"src/target\.py"):
        pi.record_review(repo, claim.claim_id, verdict="PASS", findings="ok")
    assert pi.latest_verdict(repo, session_id=SESSION).verdict == pi.VERDICT_PASS, "the earlier verdict stands as history"


def test_hook_counts_edits_only_inside_a_worktree_of_this_repo(repo: Path, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-30), "Edit", {"file_path": str(tmp_path / "memory" / "note.md")})])
    assert _hook(repo, transcript)[1].get("decision") != "block", "an Edit outside every worktree of the repo is not this repo's work"
    _transcript(transcript, [(_iso(-30), "Edit", {"file_path": str(repo / "src" / "target.py")})])
    assert _hook(repo, transcript)[1].get("decision") == "block"
    wt = tmp_path / "elsewhere-wt"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt))
    _transcript(transcript, [(_iso(-30), "Write", {"file_path": str(wt / "src" / "x.py")})])
    assert _hook(repo, transcript)[1].get("decision") == "block", "a registered worktree of the repo counts wherever it lives"


def test_hook_counts_a_foreground_subagents_work_once_its_tool_result_is_back(repo: Path, tmp_path: Path) -> None:
    """Agent-tool subagents hand back via the parent's tool_result, not an idle notification (round four, 55 % of real
    subagent transcripts). A subagent whose metadata says neither is counted in full: unknown shapes fail closed."""
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-40), "Agent", {"prompt": "fix it"}, "toolu_spawn1")])
    sub = tmp_path / "t" / "subagents" / "agent-fg1.jsonl"
    sub.parent.mkdir(parents=True)
    (sub.parent / "agent-fg1.meta.json").write_text(
        json.dumps({"toolUseId": "toolu_spawn1", "agentType": "general-purpose"}), encoding="utf-8"
    )
    _transcript(sub, [(_iso(-30), "Edit", {"file_path": str(repo / "src" / "target.py")})], final="edited")
    assert _hook(repo, transcript)[1].get("decision") != "block", "still running: nothing handed back yet"
    _tool_result(transcript, "toolu_spawn1", _iso(-20))
    assert _hook(repo, transcript)[1].get("decision") == "block", "handed back: the subagent's Edit is this session's work"
    unknown = tmp_path / "t" / "subagents" / "agent-mystery.jsonl"
    (unknown.with_suffix(".meta.json")).write_text(json.dumps({"agentType": "?"}), encoding="utf-8")
    _transcript(transcript, [(_iso(-40), "Read", {"file_path": "x"})])
    _transcript(unknown, [(_iso(-30), "Edit", {"file_path": str(repo / "src" / "target.py")})], final="edited")
    shutil.rmtree(sub.parent / "agent-fg1.jsonl", ignore_errors=True)
    (sub.parent / "agent-fg1.jsonl").unlink(missing_ok=True)
    (sub.parent / "agent-fg1.meta.json").unlink(missing_ok=True)
    assert _hook(repo, transcript)[1].get("decision") == "block", "no hand-back channel known: counted in full"
