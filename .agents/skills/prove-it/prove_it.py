#!/usr/bin/env python3
"""prove-it mechanism: falsifiable assertions, fresh-worktree verification, mutation checks, verdict files.

A completion claim is decomposed into typed assertions, each of which this module tries to
FALSIFY with an instrument that does not consult the working directory:

* ``test: <cmd>``, ``exit0: <cmd>``, ``nonzero: <cmd>`` — run in a fresh detached worktree at the
  branch tip (committed state only; uncommitted work is a false positive by construction);
* ``commit: <sha> on <branch>`` — ``git rev-parse`` + ``git merge-base --is-ancestor``;
* ``file: <path>`` — present at the branch tip, not merely on disk;
* ``mutation: <cmd> :: <paths> [@ <base>]`` — revert the fix and prove the test goes RED. An
  uncommitted fix is reverted IN PLACE for the named paths only, from a byte snapshot: the HEAD
  content is written over each path, the test runs, the snapshot bytes are written back and
  compared. Nothing else in the working directory is touched and no history verb runs — this
  repository hard-blocks ``git stash`` (silent work loss) and forbids ``git checkout --`` for
  restores. A committed fix is reverted from ``<base>`` inside a fresh worktree instead.

The verdict is written to ``.verify/<timestamp>.md``. It is ``FAIL`` if any assertion is
unproven, ``UNREVIEWED`` when every deterministic check passed but the adversarial review has
not been recorded, and ``PASS`` only when both agree. ``stop_hook.py`` next to this file reads
these verdicts and refuses to let a session that did work stop without a PASS or an explicit,
reasoned withdrawal.

Run ``python prove_it.py --help`` for the CLI.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_UNREVIEWED = "UNREVIEWED"
REVIEW_NONE = "none"

KINDS = ("test", "commit", "mutation", "file", "exit0", "nonzero")
MAX_BLOCKS = 5  # the Stop hook releases a session after this many blocks so it cannot loop forever
SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")
# RED is a FAILED ASSERTION, visible in the output. Exit 1 alone proves nothing: pytest, unittest and a plain script all
# report an uncaught exception inside a test as exit 1 too. Keyed on the property "an assertion failed" (pytest's
# `assert` / `Failed:` lines, unittest's `FAIL:`, the AssertionError itself), never on a list of crash exception names.
RED_RE = re.compile(r"AssertionError|^E\s+assert\b|- assert\b|\bFailed: |^FAIL: ", re.MULTILINE)


def now() -> datetime:
    return datetime.now(UTC)


def iso(when: datetime | None = None) -> str:
    return (when or now()).isoformat(timespec="microseconds")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def session_id_from_env() -> str:
    for name in SESSION_ENV:
        value = os.environ.get(name)
        if value:
            return value
    return "no-session"


def repo_root(start: Path) -> Path:
    proc = _git(start, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        raise RuntimeError(f"{start} is not inside a git repository")
    return Path(proc.stdout.strip())


def verify_dir(repo: Path) -> Path:
    return repo / ".verify"


# ---------------------------------------------------------------- claims


def parse_assertion(index: int, text: str) -> dict[str, object]:
    """``kind: payload`` → typed assertion. Raises ValueError with the fix in the message."""
    kind, sep, payload = text.partition(":")
    kind = kind.strip().lower()
    payload = payload.strip()
    if not sep or kind not in KINDS:
        raise ValueError(f"assertion {index}: unknown kind {kind!r}; kinds are {', '.join(KINDS)} (form 'kind: payload')")
    if not payload:
        raise ValueError(f"assertion {index}: empty payload for {kind}")
    base: dict[str, object] = {"id": f"A{index}", "kind": kind}
    if kind in {"test", "exit0", "nonzero"}:
        base["command"] = payload
    elif kind == "commit":
        sha, on, branch = payload.partition(" on ")
        if not on or not branch.strip():
            raise ValueError(f"assertion {index}: commit needs '<sha> on <branch>'")
        base["sha"] = sha.strip()
        base["branch"] = branch.strip()
    elif kind == "file":
        base["path"] = payload
    else:  # mutation
        command, sep2, rest = payload.partition("::")
        if not sep2 or not rest.strip():
            raise ValueError(f"assertion {index}: mutation needs '<test cmd> :: <path>[,<path>] [@ <base>]'")
        paths_part, at, base_ref = rest.partition("@")
        paths = [p.strip() for p in paths_part.split(",") if p.strip()]
        if not paths:
            raise ValueError(f"assertion {index}: mutation names no paths")
        base["command"] = command.strip()
        base["paths"] = paths
        base["base"] = base_ref.strip() if at and base_ref.strip() else None
    return base


@dataclass
class Claim:
    claim_id: str
    session_id: str
    created_at: str
    claim: str
    repo: str
    branch: str
    head_sha: str
    assertions: list[dict[str, object]]
    verdicts: list[dict[str, object]] = field(default_factory=list)
    withdrawn: dict[str, object] | None = None
    last_results: list[dict[str, object]] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return verify_dir(Path(self.repo)) / "claims" / f"{self.claim_id}.json"

    def save(self) -> None:
        _atomic_write(self.path, json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")

    @classmethod
    def load(cls, repo: Path, claim_id: str) -> Claim:
        path = verify_dir(repo) / "claims" / f"{claim_id}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is not dict:
            raise ValueError(f"{path}: claim root must be a mapping")
        return cls(
            claim_id=str(raw["claim_id"]),
            session_id=str(raw["session_id"]),
            created_at=str(raw["created_at"]),
            claim=str(raw["claim"]),
            repo=str(raw["repo"]),
            branch=str(raw["branch"]),
            head_sha=str(raw["head_sha"]),
            assertions=list(raw["assertions"]),
            verdicts=list(raw.get("verdicts", [])),
            withdrawn=raw.get("withdrawn"),
            last_results=list(raw.get("last_results", [])),
        )


def create_claim(*, repo: Path, session_id: str, claim: str, assertions: list[str]) -> Claim:
    repo = repo_root(Path(repo))
    if not assertions:
        raise ValueError("a claim needs at least one falsifiable assertion")
    parsed = [parse_assertion(index, text) for index, text in enumerate(assertions, start=1)]
    branch = _git(repo, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or "HEAD"
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    when = now()
    digest = hashlib.sha256(f"{session_id}{when.isoformat()}{claim}".encode()).hexdigest()[:8]
    record = Claim(
        claim_id=f"{when.strftime('%Y%m%dT%H%M%S')}-{digest}",
        session_id=session_id,
        created_at=iso(when),
        claim=claim,
        repo=str(repo),
        branch=branch,
        head_sha=head,
        assertions=parsed,
    )
    record.save()
    return record


def claims_for_session(repo: Path, session_id: str) -> list[Claim]:
    folder = verify_dir(repo) / "claims"
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob("*.json")):
        with contextlib.suppress(ValueError, KeyError, json.JSONDecodeError):
            record = Claim.load(repo, path.stem)
            if record.session_id == session_id:
                found.append(record)
    return sorted(found, key=lambda c: c.created_at)


def withdraw_session(repo: Path, *, session_id: str, reason: str) -> Claim:
    """Withdraw without a claim: the session did work, is NOT claiming it, and says why. Releases the Stop hook."""
    repo = repo_root(Path(repo))
    when = now()
    digest = hashlib.sha256(f"{session_id}{when.isoformat()}withdraw".encode()).hexdigest()[:8]
    record = Claim(
        claim_id=f"{when.strftime('%Y%m%dT%H%M%S')}-{digest}",
        session_id=session_id,
        created_at=iso(when),
        claim="(withdrawn without a claim)",
        repo=str(repo),
        branch=_git(repo, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or "HEAD",
        head_sha=_git(repo, "rev-parse", "HEAD").stdout.strip(),
        assertions=[],
        withdrawn={"at": iso(when), "reason": reason},
    )
    record.save()
    return record


def withdraw_claim(repo: Path, claim_id: str, *, reason: str) -> Claim:
    record = Claim.load(repo_root(Path(repo)), claim_id)
    record.withdrawn = {"at": iso(), "reason": reason}
    record.save()
    return record


# ---------------------------------------------------------------- verification


@dataclass
class AssertionResult:
    id: str
    kind: str
    text: str
    proven: bool
    evidence: str
    where: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class Verdict:
    claim_id: str
    session_id: str
    checked_at: str
    verdict: str
    review: str
    results: list[AssertionResult]
    unproven: list[str]
    path: str
    findings: str = ""

    def as_dict(self) -> dict[str, object]:
        raw = asdict(self)
        raw["results"] = [r.as_dict() for r in self.results]
        return raw


@contextlib.contextmanager
def _fresh_worktree(repo: Path, ref: str) -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="prove-it-"))
    path = root / "tree"
    proc = _git(repo, "worktree", "add", "--detach", str(path), ref)
    if proc.returncode != 0:
        shutil.rmtree(root, ignore_errors=True)
        raise RuntimeError(f"could not create fresh worktree at {ref}: {proc.stderr.strip()}")
    try:
        venv = repo / ".venv"
        if venv.exists():
            os.symlink(venv, path / ".venv")
        yield path
    finally:
        _git(repo, "worktree", "remove", "--force", str(path))
        _git(repo, "worktree", "prune")
        shutil.rmtree(root, ignore_errors=True)


def _run(command: str, cwd: Path, timeout: int) -> tuple[int | None, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{cwd / 'src'}:{cwd / 'elspeth-lints' / 'src'}"
    # Python trusts a .pyc by source mtime (1 s granularity) + size: the pre-revert run would compile the FIX, and a
    # same-size revert in the same second would then execute the fix's bytecode and read as "did not go red".
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(shlex.split(command), cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return None, f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    return proc.returncode, (proc.stdout + proc.stderr)[-3000:]


def _uncommitted(repo: Path, paths: list[str] | None = None) -> list[str]:
    proc = _git(repo, "status", "--porcelain", "--", *(paths or []))
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _describe(a: dict[str, object]) -> str:
    kind = a["kind"]
    if kind == "commit":
        return f"{a['sha']} on {a['branch']}"
    if kind == "file":
        return str(a["path"])
    if kind == "mutation":
        return f"{a['command']} :: {', '.join(a['paths'])}" + (f" @ {a['base']}" if a.get("base") else "")
    return str(a["command"])


def _check_command(repo: Path, tree: Path, tip: str, a: dict[str, object], timeout: int) -> AssertionResult:
    kind = str(a["kind"])
    command = str(a["command"])
    rc, tail = _run(command, tree, timeout)
    want_zero = kind in {"test", "exit0"}
    proven = rc is not None and ((rc == 0) if want_zero else (rc != 0))
    evidence = f"exit {rc} in fresh worktree at {tip[:12]} (expected {'0' if want_zero else 'non-zero'})"
    if not proven:
        dirty = _uncommitted(repo)
        if dirty:
            evidence += (
                f"; the working directory has {len(dirty)} uncommitted change(s) ({', '.join(d.strip() for d in dirty[:5])})"
                " — uncommitted work is not on the branch"
            )
        evidence += f"; tail: {tail.strip()[-300:]}" if tail.strip() else ""
    return AssertionResult(id=str(a["id"]), kind=kind, text=command, proven=proven, evidence=evidence, where=str(tree))


def _check_commit(repo: Path, a: dict[str, object]) -> AssertionResult:
    sha, branch = str(a["sha"]), str(a["branch"])
    resolved = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if resolved.returncode != 0:
        return AssertionResult(
            str(a["id"]), "commit", _describe(a), False, f"{sha} does not resolve to a commit (git rev-parse)", str(repo)
        )
    full = resolved.stdout.strip()
    tip = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if tip.returncode != 0:
        return AssertionResult(str(a["id"]), "commit", _describe(a), False, f"branch {branch} does not exist", str(repo))
    tip_sha = tip.stdout.strip()
    ancestor = _git(repo, "merge-base", "--is-ancestor", full, tip_sha).returncode == 0
    if not ancestor:
        return AssertionResult(
            str(a["id"]), "commit", _describe(a), False, f"{full[:12]} is not an ancestor of {branch} tip {tip_sha[:12]}", str(repo)
        )
    behind = _git(repo, "rev-list", "--count", f"{full}..{tip_sha}").stdout.strip()
    position = "the tip" if full == tip_sha else f"{behind} commit(s) behind the tip {tip_sha[:12]}"
    return AssertionResult(str(a["id"]), "commit", _describe(a), True, f"{full[:12]} resolves and is on {branch}: {position}", str(repo))


def _check_file(repo: Path, tip: str, a: dict[str, object]) -> AssertionResult:
    path = str(a["path"])
    at_tip = _git(repo, "cat-file", "-e", f"{tip}:{path}").returncode == 0
    if at_tip:
        return AssertionResult(str(a["id"]), "file", path, True, f"present at tip {tip[:12]}", str(repo))
    on_disk = (repo / path).exists()
    evidence = f"not at the tip {tip[:12]}" + (" (exists in the working directory: uncommitted)" if on_disk else " and not on disk")
    return AssertionResult(str(a["id"]), "file", path, False, evidence, str(repo))


def _revert_in_place(repo: Path, paths: list[str]) -> dict[str, bytes | None]:
    """Snapshot each path's bytes, then write the HEAD version over it (or remove it if HEAD lacks it)."""
    snapshot: dict[str, bytes | None] = {}
    for rel in paths:
        target = repo / rel
        snapshot[rel] = target.read_bytes() if target.exists() else None
    for rel in paths:
        target = repo / rel
        head = subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{rel}"], capture_output=True, check=False)
        if head.returncode == 0:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(head.stdout)
        elif target.exists():
            target.unlink()
    return snapshot


def _restore_in_place(repo: Path, snapshot: dict[str, bytes | None]) -> bool:
    for rel, content in snapshot.items():
        target = repo / rel
        if content is None:
            if target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    return all(((repo / rel).read_bytes() if (repo / rel).exists() else None) == content for rel, content in snapshot.items())


def _interpret_red(rc: int | None, tail: str, note: str) -> tuple[bool, str]:
    """RED means the test FAILED AN ASSERTION: exit 1 AND a failed assertion visible in the output.

    0 = not red; any other exit = crashed; exit 1 without an assertion in the output = an uncaught exception that the
    runner reported as a failure (pytest, unittest and plain scripts all do), which never ran the assertion either.
    """
    if rc is None:
        return False, f"{note}; test could not run ({tail})"
    if rc == 0:
        return False, f"{note}; test did not go red (exit 0): the test does not depend on the fix"
    if rc != 1:
        return False, f"{note}; test crashed rather than failed (exit {rc}): RED means a failed assertion (exit 1)"
    if not RED_RE.search(tail):
        return (
            False,
            f"{note}; test failed with exit 1 but no failed assertion in its output (an uncaught exception is a crash, not RED): "
            "assert what the fix adds exists (importlib.util.find_spec / hasattr) so the assertion is what fails; "
            f"tail: {tail.strip()[-300:]}",
        )
    return True, f"{note}; test went red (exit 1, failed assertion)"


def _check_mutation(repo: Path, tip: str, a: dict[str, object], timeout: int) -> AssertionResult:
    command = str(a["command"])
    paths = [str(p) for p in a["paths"]]
    base = a.get("base")
    aid = str(a["id"])
    text = _describe(a)
    if _uncommitted(repo, paths):
        # Working-directory mode: the fix is uncommitted, so the working directory IS the subject.
        # Only the named paths are touched, from a byte snapshot; nothing else is read or written.
        links = [rel for rel in paths if (repo / rel).is_symlink() or _git(repo, "ls-files", "-s", "--", rel).stdout.startswith("120000")]
        if links:
            evidence = (
                f"{links} is a symlink (in the working directory or at HEAD): HEAD's blob is the link-target string and a revert "
                "would write it THROUGH the link into another file; name the real file instead"
            )
            return AssertionResult(aid, "mutation", text, False, evidence, str(repo))
        pre_rc, pre_tail = _run(command, repo, timeout)
        if pre_rc != 0:
            evidence = f"command does not pass on the unmodified working directory (exit {pre_rc}): a later non-zero exit would prove nothing; {pre_tail.strip()[-200:]}"
            return AssertionResult(aid, "mutation", text, False, evidence, str(repo))
        snapshot = _revert_in_place(repo, paths)
        try:
            rc, tail = _run(command, repo, timeout)
        finally:
            restored = _restore_in_place(repo, snapshot)
        note = f"passes before the revert (exit 0); reverted {paths} to HEAD in place from a byte snapshot; restored " + (
            "byte-identical" if restored else "WITH DIFFERENCES — inspect"
        )
        proven, evidence = _interpret_red(rc, tail, note)
        return AssertionResult(aid, "mutation", text, proven and restored, evidence, str(repo))
    if not base:
        evidence = "the fix is committed: name the pre-fix base with '@ <base>' so it can be reverted in a fresh worktree"
        return AssertionResult(aid, "mutation", text, False, evidence, str(repo))
    base_sha = _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if base_sha.returncode != 0:
        return AssertionResult(aid, "mutation", text, False, f"base {base} does not resolve", str(repo))
    with _fresh_worktree(repo, tip) as tree:
        pre_rc, pre_tail = _run(command, tree, timeout)
        if pre_rc != 0:
            evidence = f"command does not pass on the unmodified tree at {tip[:12]} (exit {pre_rc}): a later non-zero exit would prove nothing; {pre_tail.strip()[-200:]}"
            return AssertionResult(aid, "mutation", text, False, evidence, str(tree))
        # The pre-fix state of a path the base never had is ABSENCE: delete it rather than fail the checkout.
        base_commit = base_sha.stdout.strip()
        present = [rel for rel in paths if _git(repo, "cat-file", "-e", f"{base_commit}:{rel}").returncode == 0]
        absent = [rel for rel in paths if rel not in present]
        if present:
            revert = _git(tree, "checkout", base_commit, "--", *present)
            if revert.returncode != 0:
                return AssertionResult(
                    aid, "mutation", text, False, f"could not restore {present} from {base}: {revert.stderr.strip()[-300:]}", str(tree)
                )
        for rel in absent:
            target = tree / rel
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        rc, tail = _run(command, tree, timeout)
        note = f"passes at {tip[:12]} before the revert (exit 0); in a fresh worktree with {present} restored from {base}" + (
            f" and {absent} removed (absent at {base})" if absent else ""
        )
        proven, evidence = _interpret_red(rc, tail, note)
        return AssertionResult(aid, "mutation", text, proven, evidence, str(tree))


def verify_claim(repo: Path, claim_id: str, *, timeout: int = 1800) -> Verdict:
    repo = repo_root(Path(repo))
    record = Claim.load(repo, claim_id)
    tip_proc = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{record.branch}")
    tip = tip_proc.stdout.strip() if tip_proc.returncode == 0 else _git(repo, "rev-parse", "HEAD").stdout.strip()
    results: list[AssertionResult] = []
    tree_needed = any(a["kind"] in {"test", "exit0", "nonzero"} for a in record.assertions)
    with _fresh_worktree(repo, tip) if tree_needed else contextlib.nullcontext(repo) as tree:
        for a in record.assertions:
            kind = a["kind"]
            if kind in {"test", "exit0", "nonzero"}:
                results.append(_check_command(repo, tree, tip, a, timeout))
            elif kind == "commit":
                results.append(_check_commit(repo, a))
            elif kind == "file":
                results.append(_check_file(repo, tip, a))
            else:
                results.append(_check_mutation(repo, tip, a, timeout))
    record.last_results = [r.as_dict() for r in results]
    record.save()
    return _write_verdict(repo, record, results, review=REVIEW_NONE, findings="")


def record_review(repo: Path, claim_id: str, *, verdict: str, findings: str) -> Verdict:
    repo = repo_root(Path(repo))
    record = Claim.load(repo, claim_id)
    if verdict not in {VERDICT_PASS, VERDICT_FAIL}:
        raise ValueError("review verdict must be PASS or FAIL")
    if not record.last_results:
        raise ValueError(f"claim {claim_id} has not been verified; run verify first")
    results = [AssertionResult(**r) for r in record.last_results]
    return _write_verdict(repo, record, results, review=verdict, findings=findings)


def _write_verdict(repo: Path, record: Claim, results: list[AssertionResult], *, review: str, findings: str) -> Verdict:
    unproven = [r.id for r in results if not r.proven]
    if unproven or review == VERDICT_FAIL:
        final = VERDICT_FAIL
    elif review == VERDICT_PASS:
        final = VERDICT_PASS
    else:
        final = VERDICT_UNREVIEWED
    when = now()
    path = verify_dir(repo) / f"{when.strftime('%Y%m%dT%H%M%S%f')}Z.md"
    lines = [
        f"# prove-it verdict — {final}",
        f"claim: {record.claim_id}",
        f"session: {record.session_id}",
        f"checked_at: {iso(when)}",
        f"verdict: {final}",
        f"review: {review}",
        f"branch: {record.branch} @ {record.head_sha[:12]} (claimed)",
        "",
        "## Claim",
        "",
        record.claim,
        "",
        "## Assertions",
        "",
    ]
    for r in results:
        lines.append(f"- {r.id} [{r.kind}] {'PROVEN' if r.proven else 'UNPROVEN'} — `{r.text}` — {r.evidence}")
    lines += ["", "## Unproven", ""]
    if unproven:
        for r in results:
            if not r.proven:
                lines.append(f"- {r.id}: {r.text} — {r.evidence}")
        lines.append("")
        lines.append("These claims could not be substantiated. Do not report success; report exactly this list.")
    else:
        lines.append("_none_")
    lines += ["", "## Adversarial review", "", f"verdict: {review}", "", findings or "_not yet recorded_", ""]
    _atomic_write(path, "\n".join(lines))
    entry = {"path": str(path), "checked_at": iso(when), "verdict": final, "review": review, "unproven": unproven}
    record.verdicts.append(entry)
    record.save()
    return Verdict(
        claim_id=record.claim_id,
        session_id=record.session_id,
        checked_at=str(entry["checked_at"]),
        verdict=final,
        review=review,
        results=results,
        unproven=unproven,
        path=str(path),
        findings=findings,
    )


def latest_verdict(repo: Path, *, session_id: str) -> Verdict | None:
    """Newest verdict across the session's claims, or None. Only verdicts whose file still exists count."""
    best: tuple[str, Claim, dict[str, object]] | None = None
    for record in claims_for_session(repo, session_id):
        for entry in record.verdicts:
            if not Path(str(entry["path"])).is_file():
                continue
            if best is None or str(entry["checked_at"]) > best[0]:
                best = (str(entry["checked_at"]), record, entry)
    if best is None:
        return None
    _, record, entry = best
    results = [AssertionResult(**r) for r in record.last_results]
    return Verdict(
        claim_id=record.claim_id,
        session_id=record.session_id,
        checked_at=str(entry["checked_at"]),
        verdict=str(entry["verdict"]),
        review=str(entry["review"]),
        results=results,
        unproven=[str(x) for x in entry["unproven"]],
        path=str(entry["path"]),
    )


# ---------------------------------------------------------------- CLI


def _cmd_claim(args: argparse.Namespace) -> int:
    record = create_claim(
        repo=Path(args.repo), session_id=args.session or session_id_from_env(), claim=args.claim, assertions=args.assertions
    )
    print(
        json.dumps(
            {"claim_id": record.claim_id, "session_id": record.session_id, "path": str(record.path), "assertions": record.assertions},
            indent=2,
        )
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    verdict = verify_claim(Path(args.repo), args.claim, timeout=args.timeout)
    print(json.dumps(verdict.as_dict(), indent=2))
    return 0 if verdict.verdict == VERDICT_PASS else 1


def _cmd_review(args: argparse.Namespace) -> int:
    findings = Path(args.findings).read_text(encoding="utf-8") if args.findings and Path(args.findings).is_file() else (args.findings or "")
    verdict = record_review(Path(args.repo), args.claim, verdict=args.verdict, findings=findings)
    print(json.dumps(verdict.as_dict(), indent=2))
    return 0 if verdict.verdict == VERDICT_PASS else 1


def _cmd_withdraw(args: argparse.Namespace) -> int:
    if args.claim:
        record = withdraw_claim(Path(args.repo), args.claim, reason=args.reason)
    else:
        record = withdraw_session(Path(args.repo), session_id=args.session or session_id_from_env(), reason=args.reason)
    print(json.dumps({"claim_id": record.claim_id, "withdrawn": record.withdrawn}, indent=2))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    repo = repo_root(Path(args.repo))
    session = args.session or session_id_from_env()
    verdict = latest_verdict(repo, session_id=session)
    claims = claims_for_session(repo, session)
    print(
        json.dumps(
            {
                "session_id": session,
                "claims": [{"claim_id": c.claim_id, "claim": c.claim, "withdrawn": c.withdrawn, "verdicts": c.verdicts} for c in claims],
                "verdict": verdict.verdict if verdict else None,
                "verdict_path": verdict.path if verdict else None,
                "unproven": verdict.unproven if verdict else [],
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="any path inside the repository (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("claim", help="decompose a completion claim into typed, falsifiable assertions")
    p.add_argument("--claim", required=True, help="the completion claim in one sentence")
    p.add_argument(
        "--assert", dest="assertions", action="append", required=True, help="'kind: payload'; repeatable. kinds: " + ", ".join(KINDS)
    )
    p.add_argument("--session", default=None, help="session id (default: $CLAUDE_CODE_SESSION_ID)")
    p.set_defaults(func=_cmd_claim)

    p = sub.add_parser("verify", help="try to falsify every assertion; write .verify/<timestamp>.md; exit 0 only on PASS")
    p.add_argument("--claim", required=True)
    p.add_argument("--timeout", type=int, default=1800)
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("review", help="record the adversarial reviewer's verdict and findings against a verified claim")
    p.add_argument("--claim", required=True)
    p.add_argument("--verdict", required=True, choices=[VERDICT_PASS, VERDICT_FAIL])
    p.add_argument("--findings", required=True, help="path to the reviewer's findings file, or the findings text")
    p.set_defaults(func=_cmd_review)

    p = sub.add_parser(
        "withdraw", help="withdraw a claim you cannot substantiate, or (no --claim) record that this session is not claiming its work"
    )
    p.add_argument("--claim", default=None)
    p.add_argument("--reason", required=True)
    p.add_argument("--session", default=None, help="session id for a claim-less withdrawal (default: $CLAUDE_CODE_SESSION_ID)")
    p.set_defaults(func=_cmd_withdraw)

    p = sub.add_parser("status", help="this session's claims and latest verdict")
    p.add_argument("--session", default=None)
    p.set_defaults(func=_cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
