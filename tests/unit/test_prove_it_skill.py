"""Contract tests for the prove-it skill: falsifiable assertions, fresh-worktree verification,
snapshot-roundtrip mutation checks, verdict files, and the Stop hook that refuses an unproven completion.

Real git repositories and real subprocesses throughout — the mechanism's whole point is that it
does not take anyone's word, so these tests do not take the mechanism's either.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
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
GOOD_TEST = "import sys\nsys.exit(0 if open('src/target.py').read().strip() == 'VALUE = 2' else 1)\n"


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
    (repo / "tests" / "test_helper.py").write_text(
        "import sys, os\nsys.exit(0 if os.path.exists('src/helper.py') else 1)\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat: helper (new file)")
    claim = _claim(repo, f"mutation: {sys.executable} tests/test_helper.py :: src/helper.py @ main")
    verdict = pi.verify_claim(repo, claim.claim_id)
    assert verdict.results[0].proven is True, verdict.results[0].evidence
    assert "absent" in verdict.results[0].evidence
    assert (repo / "src" / "helper.py").is_file(), "the checkout is never touched"


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
        assert _hook(repo, transcript)[1].get("decision") == "block", claim


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


def test_hook_releases_a_withdrawn_claim_and_ignores_other_sessions(repo: Path, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-60), "Edit", {"file_path": "src/target.py"})])
    claim = _claim(repo, "file: missing.txt")
    pi.verify_claim(repo, claim.claim_id)
    assert _hook(repo, transcript)[1]["decision"] == "block"
    pi.withdraw_claim(repo, claim.claim_id, reason="cannot substantiate; reporting incomplete")
    code, out, _ = _hook(repo, transcript)
    assert code == 0 and out.get("decision") != "block"
    assert "withdrawn" in out.get("systemMessage", "").lower()
    # a different session's open, failing claim never blocks this one
    other = _claim(repo, "file: missing.txt", session=OTHER_SESSION)
    pi.verify_claim(repo, other.claim_id)
    _transcript(transcript, [(_iso(-30), "Read", {"file_path": "x"})])
    assert _hook(repo, transcript)[1].get("decision") != "block"


def test_hook_caps_repeated_blocks_so_a_session_cannot_loop_forever(repo: Path, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _transcript(transcript, [(_iso(-60), "Edit", {"file_path": "src/target.py"})])
    decisions = [_hook(repo, transcript, active=True)[1] for _ in range(pi.MAX_BLOCKS + 1)]
    assert all(d["decision"] == "block" for d in decisions[: pi.MAX_BLOCKS])
    assert decisions[-1].get("decision") != "block"
    assert "NOT verified" in decisions[-1]["systemMessage"]


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
        "test:",
        "commit:",
        "mutation:",
        "exit0:",
        "nonzero:",
        "file:",
    ):
        assert token in text, token
