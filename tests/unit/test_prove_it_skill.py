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


TEST_CMD = f"{sys.executable} tests/test_target.py"
GOOD_TEST = "assert open('src/target.py').read().strip() == 'VALUE = 2'\n"  # RED is a FAILED ASSERTION, visible in the output


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
    (repo / "tests" / "test_target.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")  # a test that cannot fail
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
    (repo / "tests" / "test_helper.py").write_text("import os\n\nassert os.path.exists('src/helper.py')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: helper (new file)")
    claim = _claim(repo, f"mutation: {sys.executable} tests/test_helper.py :: src/helper.py @ main")
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
    (repo / "tests" / "test_target.py").write_text("assert open('src/target.py').read().strip() == 'VALUE = 3'\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "test: wants 3 (red at the tip)")
    (repo / "tests" / "test_target.py").write_text(GOOD_TEST, encoding="utf-8")  # uncommitted: the checkout is green
    assert subprocess.run(shlex.split(TEST_CMD), cwd=repo, check=False).returncode == 0
    claim = _claim(repo, f"mutation: {TEST_CMD} :: src/target.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False
    assert "does not pass on the unmodified tree" in result.evidence, result.evidence


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
    """Every common runner reports an uncaught exception as exit 1 (round two). RED is a FAILED ASSERTION visible in the
    output; a test that merely imports what the fix adds proves nothing about the fix."""
    body, command = EXIT_1_CRASH_SHAPES[shape]
    (repo / "src" / "helper.py").write_text("HELPED = True\n", encoding="utf-8")
    (repo / "tests" / "test_helper.py").write_text(body, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: helper + a test that asserts nothing")
    claim = _claim(repo, f"mutation: {command} :: src/helper.py @ main")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False, result.evidence
    assert "exit 1" in result.evidence and "assertion" in result.evidence.lower(), result.evidence


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
    command = f"{sys.executable} -c 'import sys; assert sys.dont_write_bytecode; import target; assert target.VALUE == 2'"
    claim = _claim(repo, f"mutation: {command} :: src/target.py")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is True, result.evidence
    assert not (repo / "src" / "__pycache__").exists()


def test_mutation_refuses_a_symlink_path_rather_than_writing_through_it(repo: Path) -> None:
    """HEAD's blob for a symlink is the link TARGET string; writing it through the link corrupts the file it points at,
    and that corruption is what would make the test 'go red' (round two)."""
    (repo / "src" / "real.py").write_text("REAL = 1\n", encoding="utf-8")
    os.symlink("real.py", repo / "src" / "link.py")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "link.py -> real.py")
    (repo / "src" / "link.py").unlink()
    os.symlink("other.py", repo / "src" / "link.py")  # uncommitted: now points at other.py
    command = f"{sys.executable} -c \"assert open('src/other.py').read() == 'OTHER = 1\\n'\""
    claim = _claim(repo, f"mutation: {command} :: src/link.py")
    result = pi.verify_claim(repo, claim.claim_id).results[0]
    assert result.proven is False and "symlink" in result.evidence, result.evidence
    assert (repo / "src" / "other.py").read_text(encoding="utf-8") == "OTHER = 1\n"
    assert os.readlink(repo / "src" / "link.py") == "other.py"


def test_mutation_that_crashes_instead_of_failing_is_unproven(repo: Path) -> None:
    """RED means the test FAILED an assertion (exit 1); a crash (any other exit) never ran the assertion."""
    (repo / "tests" / "test_target.py").write_text(
        "import sys\nsys.exit(0 if open('src/target.py').read().strip() == 'VALUE = 2' else 2)\n", encoding="utf-8"
    )
    _git(repo, "commit", "-q", "-am", "test: exits 2 when unhappy")
    _commit_fix(repo)
    claim = _claim(repo, f"mutation: {TEST_CMD} :: src/target.py @ main")
    verdict = pi.verify_claim(repo, claim.claim_id)
    assert verdict.results[0].proven is False
    assert "crashed" in verdict.results[0].evidence and "exit 2" in verdict.results[0].evidence


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
    for when, tool, tool_input in events:
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": when,
                    "sessionId": SESSION,
                    "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": tool, "input": tool_input}]},
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


def test_hook_lets_a_session_yield_when_its_last_message_claims_nothing(repo: Path, tmp_path: Path) -> None:
    """Unproven work + a final message that only reports status (waiting, next steps) is not a completion claim."""
    transcript = tmp_path / "t.jsonl"
    _transcript(
        transcript,
        [(_iso(-30), "Edit", {"file_path": "src/target.py"})],
        final="Gate still running at 85%; waiting for the marker before I do anything else.",
    )
    code, out, _ = _hook(repo, transcript)
    assert code == 0 and out.get("decision") != "block"
    for claim in (
        "All done.",
        "The bug is fixed.",
        "Tests are green now.",
        "Committed as abc123.",
        "Merged into main.",
        "This is complete.",
    ):
        _transcript(transcript, [(_iso(-30), "Edit", {"file_path": "src/target.py"})], final=claim)
        shutil.rmtree(repo / ".verify" / ".hook", ignore_errors=True)  # each phrase is judged on its own, not by the valve
        assert _hook(repo, transcript)[1].get("decision") == "block", claim
    for honest in (
        "I could not fix this. The work is NOT done and the test still fails.",
        "Reporting this as incomplete: the mutation did not go red, so the fix is unverified.",
        "This is not fixed yet; the pipeline works by reading the manifest but I have changed nothing that matters.",
    ):
        _transcript(transcript, [(_iso(-30), "Edit", {"file_path": "src/target.py"})], final=honest)
        assert _hook(repo, transcript)[1].get("decision") != "block", honest


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
    assert out["decision"] == "block" and "verify" in out["reason"]
    pi.verify_claim(repo, claim.claim_id)  # UNREVIEWED
    code, out, _ = _hook(repo, transcript)
    assert out["decision"] == "block" and "review" in out["reason"].lower()
    pi.record_review(repo, claim.claim_id, verdict="FAIL", findings="A1 flaky")
    code, out, _ = _hook(repo, transcript)
    assert out["decision"] == "block" and "FAIL" in out["reason"] and "withdraw" in out["reason"]
    pi.record_review(repo, claim.claim_id, verdict="PASS", findings="could not falsify")
    code, out, _ = _hook(repo, transcript)
    assert code == 0 and out.get("decision") != "block"
    # new work after the PASS re-arms the gate
    _transcript(transcript, [(_iso(-60), "Edit", {"file_path": "src/target.py"}), (_iso(30), "Write", {"file_path": "src/new.py"})])
    assert _hook(repo, transcript)[1]["decision"] == "block"


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


SMUGGLED_CLAIMS = [  # a completion claim in ONE sentence is gated whatever a later sentence takes back (round two)
    "Done and merged. The docs are incomplete but that is tracked separately.",
    "Fixed, committed and pushed. Nothing is left unverified.",
    "All green, landed on main. There is nothing undone here.",
    "Implemented and passing. Note: one flaky test is not fixed in this PR.",
    "Merged. I never touched the frontend.",
]
HONEST_REPORTS = [  # each relies on exactly ONE negation alternative, in the same sentence as the completion word
    "The suite is not yet green.",
    "This isn't fully fixed.",
    "I never got this passing.",
    "I cannot call this done.",
    "Merged, but the docs remain undone.",
    "Implemented, but incomplete.",
    "Landed, but the e2e test still fails.",
    "Giving up on this; the fix is half done.",
    "The pipeline works by reading the manifest; I have not changed anything yet.",
    "Here is how it works: the resolver reads the manifest.",
]


@pytest.mark.parametrize("text", SMUGGLED_CLAIMS)
def test_hook_gates_a_claim_that_is_negated_only_in_another_sentence(repo: Path, tmp_path: Path, text: str) -> None:
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-30), "Edit", {"file_path": "src/target.py"})], final=text)
    assert _hook(repo, transcript)[1].get("decision") == "block", text


@pytest.mark.parametrize("text", HONEST_REPORTS)
def test_hook_releases_an_honest_report_negated_in_the_claiming_sentence(repo: Path, tmp_path: Path, text: str) -> None:
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-30), "Edit", {"file_path": "src/target.py"})], final=text)
    assert _hook(repo, transcript)[1].get("decision") != "block", text


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
}
NOT_WORK_COMMANDS = {
    "ls": "ls -la src/",
    "cat": "cat src/target.py",
    "grep": "grep -rn VALUE src/",
    "git-status": "git status --short && git diff --stat",
    "git-log": "git log --oneline -5",
    "pytest-plain": "pytest tests/unit/test_x.py -n 0 -q",
    "pytest-to-log-variable": 'pytest tests/ > "$log" 2>&1; echo exit=$?',
    "redirect-devnull": "make lint > /dev/null 2>&1",
    "redirect-tmp": "pytest tests/ > /tmp/run.log 2>&1",
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


def test_hook_counts_mcp_writers_workflows_and_delegated_agents_as_work(repo: Path, tmp_path: Path) -> None:
    """A parent that fans work out records only an Agent tool use; the edits live in <session>/subagents/agent-*.jsonl."""
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-30), "mcp__elspeth-judge__stage_annotate", {"finding": "x"})])
    assert _hook(repo, transcript)[1].get("decision") == "block"
    _transcript(transcript, [(_iso(-30), "mcp__loomweave__entity_find", {"q": "x"}), (_iso(-29), "Read", {"file_path": "x"})])
    assert _hook(repo, transcript)[1].get("decision") != "block"
    _transcript(transcript, [(_iso(-30), "Workflow", {"script": "..."})])
    assert _hook(repo, transcript)[1].get("decision") == "block"
    _transcript(transcript, [(_iso(-30), "Agent", {"prompt": "look around", "subagent_type": "Explore"})])
    assert _hook(repo, transcript)[1].get("decision") != "block", "an Agent use alone is not work: Explore agents edit nothing"
    sub = tmp_path / "t" / "subagents" / "agent-abc123.jsonl"
    sub.parent.mkdir(parents=True)
    _transcript(sub, [(_iso(-20), "Edit", {"file_path": "src/target.py"})], final="Edited src/target.py as asked.")
    assert _hook(repo, transcript)[1].get("decision") == "block", "the subagent's Edit is this session's work"


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
        "test:",
        "commit:",
        "mutation:",
        "exit0:",
        "nonzero:",
        "file:",
    ):
        assert token in text, token
