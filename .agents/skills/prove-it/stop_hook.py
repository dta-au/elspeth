#!/usr/bin/env python3
"""Claude Code Stop hook: refuse to let a session that did work stop without a prove-it PASS or a withdrawal.

Reads the hook payload on stdin (session_id, transcript_path, cwd), scans the session's transcript — and the
transcripts of every subagent it spawned, under ``<transcript stem>/subagents/agent-*.jsonl``, counted once that
subagent's work is handed back (the parent's tool_result for a foreground Agent call, or a teammate's idle
notification) — for WORK SIGNALS,
and compares the newest one against the session's newest release: a ``PASS`` verdict or an explicit, reasoned
withdrawal. Work newer than the last release blocks the stop with a reason that names the exact next command.
Other sessions' claims are invisible.

The hook reads NO prose. It gates on one measurable fact — a work signal newer than the last release — whatever the
final message says; a session that is waiting, or reporting honestly that it is not done, releases itself with
``prove_it.py withdraw --reason '<why>'``. (A prose classifier was tried and measured on 1507 real sessions:
58 % false positives, 6 % false negatives. The operator ruled it out on 2026-09-09.)

A work signal is anything that changes files or history: the editing tools on a path inside a worktree of this
repo; ``Workflow``; an MCP tool whose verb is NOT in a small read-only allowlist; a Bash command that commits,
merges, pushes, pulls, resets or otherwise rewrites git state (or uses a git alias), copies, moves, removes or
rewrites files, runs a formatter or fixer, a package manager, a filigree CLI verb outside its read set, ``elspeth
run --execute`` or a canonical script with ``--execute``, redirects output to a path, or runs inline Python that
writes or shells out. Redirects to ``/dev/null``, ``/tmp``, a ``scratchpad`` directory, or a shell variable are
treated as logs, not work — that narrow set is the hook's deliberate fail-open, listed here so it can be judged.
The Bash side is a heuristic and will always have gaps; the MCP and filigree sides are allowlists because their
read sets are small and stable.

After MAX_BLOCKS consecutive blocks since the same release the hook releases the session with a loud systemMessage —
a gate that can loop forever is a denial of service, not a control — and says plainly that the work is NOT verified.

Emits JSON on stdout: ``{"decision": "block", "reason": ...}`` or ``{}``/``{"systemMessage": ...}``.
Never exits non-zero on its own errors: a broken hook must not trap every session.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit", "Workflow"}  # Workflow: orchestration whose agents edit
MCP_READ_TOOLS = {"mcp__loomweave__entity_resolve"}  # exact names: 'resolve' is a writer verb in filigree (resolve_annotation)
MCP_READ_VERBS = {
    "get", "list", "find", "search", "status", "preview", "explain", "describe", "query", "at", "summary",
    "diff", "timeline", "validate", "lookup", "show", "read", "count", "check", "context", "guide", "schema", "help",
    "available", "path", "verify", "cost", "neighborhood", "callers", "source", "kind", "wardline", "hotspot",
}  # fmt: skip
GIT_PREFIX = r"(?:-C\s+\S+\s+|-c\s+\S+\s+|--git-dir=\S+\s+|--work-tree=\S+\s+)*"
GIT_ALIAS_RE = re.compile(r"\bgit\s+(?:\S+\s+)*?-c\s+alias\.")  # an alias hides its verb: fail closed
GIT_WRITE_RE = re.compile(
    r"\bgit\s+" + GIT_PREFIX + r"(commit|merge|cherry-pick|rebase|am|apply|revert|push|pull|reset|clean|restore|checkout|switch"
    r"|rm|mv|add|tag|branch\s+(?:-[dDmMf]|--force|--delete|--move)|worktree\s+(?:add|remove|prune|move)|update-ref|update-index"
    r"|symbolic-ref|filter-branch|notes|gc|prune|reflog\s+expire)\b"
)
GH_WRITE_RE = re.compile(r"\bgh\s+(?:pr|issue|release|repo)\b.*?\b(merge|create|close|edit|ready|delete|comment|reopen)\b")
GH_API_WRITE_RE = re.compile(r"\bgh\s+api\b.*?(?:-X|--method)[= ]+(?:POST|PUT|PATCH|DELETE)\b")
FILE_WRITE_RE = re.compile(
    r"(?:^|[;&|(\s])(?:sudo\s+)?(cp|mv|rm|rmdir|mkdir|touch|ln|chmod|chown|install|patch|rsync|truncate|tee|shred|dd|sponge|unzip|ed|ex)(?:\s|$)"
    r"|\bfind\b.*\s-delete\b|\btar\s+(?:-?[a-zA-Z]*x[a-zA-Z]*)\b"
)
INPLACE_EDIT_RE = re.compile(r"\b(?:sed|perl)\s+(?:\S+\s+)*?-[a-zA-Z]*i\b")
FIXER_RE = re.compile(
    r"\b(?:ruff\s+format|ruff\s+check\b.*--fix|black|isort|autopep8|autoflake|prettier\b.*--write|eslint\b.*--fix|pre-commit\s+run"
    r"|npm\s+(?:install|ci|update)|uv\s+(?:sync|lock|add|remove|pip\s+(?:install|uninstall))|pip\s+(?:install|uninstall)"
    r"|alembic\s+(?:upgrade|downgrade)|sqlite3\b.*\b(?:DELETE|UPDATE|INSERT|DROP)\b)"
)
CANONICAL_EXECUTE_RE = re.compile(r"scripts/[\w./-]+\.sh\b.*--execute\b")
ELSPETH_EXECUTE_RE = re.compile(r"\belspeth\s+run\b.*--execute\b")  # writes runtime data (AGENTS.md quick reference)
FILIGREE_READ_VERBS = {
    "list", "show", "search", "session-context", "status", "summary", "metrics", "schema", "type", "template", "help", "--help",
    "ready", "blocked", "stale", "change-list", "comment-list", "label-list", "workflow-status", "dependency-critical-path", "version",
}  # fmt: skip
FILIGREE_RE = re.compile(r"\bfiligree\s+(?:-\S+\s+)*([a-z][\w-]*)")  # the CLI is AGENTS.md's documented MCP fallback
PY_INLINE_RE = re.compile(r"\bpython[0-9.]*\s+(?:-c\b|-\s*<<)")
PY_WRITE_RE = re.compile(
    r"write_text|write_bytes|\.write\(|open\([^)]*['\"][wax]|os\.(?:remove|rename|replace|unlink|makedirs|mkdir|rmdir)\b"
    r"|shutil\.|Path\([^)]*\)\.(?:unlink|mkdir|rename|touch|replace)|subprocess\.|os\.(?:system|popen)\("
)
REDIRECT_RE = re.compile(r"(?:(?<![<>])[12]?>{1,2}|&>{1,2})\s*(?!&)(\S+)")
# Logs, not work: a shell variable, /dev/null, /tmp, or a DIRECTORY segment containing "scratchpad". Targets are
# normalised first so `/tmp/../home/...` and `src/scratchpad_utils.py` are not mistaken for logs.
LOG_TARGET_RE = re.compile(r"^(?:\$|/dev/null$|/tmp/|(?:.*/)?[^/]*scratchpad[^/]*/)")
LANE_MERGE_RE = re.compile(r"lane_manager\.py\s+merge\b")


def _load_prove_it() -> object:
    spec = importlib.util.spec_from_file_location("prove_it_hook_dep", HERE / "prove_it.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def _parse_ts(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _is_log_target(target: str) -> bool:
    cleaned = target.strip("\"'")
    if cleaned.startswith("$"):
        return True
    return bool(LOG_TARGET_RE.match(os.path.normpath(cleaned) + ("/" if cleaned.endswith("/") else "")))


def bash_is_work(command: str) -> bool:
    """Does this shell command change files or git history? Fails toward YES; the log-target set is the only fail-open.

    Every check runs on the whole command: a command that also mentions the verifier's own script gets no exemption.
    """
    if GIT_WRITE_RE.search(command) or GIT_ALIAS_RE.search(command) or GH_WRITE_RE.search(command) or GH_API_WRITE_RE.search(command):
        return True
    if LANE_MERGE_RE.search(command) or ELSPETH_EXECUTE_RE.search(command):
        return True
    if FILE_WRITE_RE.search(command) or INPLACE_EDIT_RE.search(command) or FIXER_RE.search(command) or CANONICAL_EXECUTE_RE.search(command):
        return True
    if any(verb not in FILIGREE_READ_VERBS for verb in FILIGREE_RE.findall(command)):
        return True
    if PY_INLINE_RE.search(command) and PY_WRITE_RE.search(command):
        return True
    return any(not _is_log_target(target) for target in REDIRECT_RE.findall(command))


def mcp_is_work(name: str) -> bool:
    """An MCP tool is work unless its first or last name token is a read verb (or it is an exact read tool). The read
    set is small and stable; a list of writers was incomplete three reviews running."""
    if name in MCP_READ_TOOLS:
        return False
    tokens = name.split("__")[-1].split("_")
    return not ({tokens[0], tokens[-1]} & MCP_READ_VERBS)


def _under(path: str, roots: list[Path]) -> bool:
    try:
        resolved = Path(path).resolve()
    except OSError:
        return True  # unresolvable: fail closed
    return any(resolved == root or root in resolved.parents for root in roots)


def tool_is_work(name: str, tool_input: dict[str, object], worktrees: list[Path]) -> bool:
    """``worktrees``: every registered worktree of the repo. An edit outside all of them (a memory note, another
    project) is not this repo's work; an edit whose path is missing from the input counts (fail closed)."""
    if name in WORK_TOOLS:
        target = tool_input.get("file_path") or tool_input.get("notebook_path")
        if name != "Workflow" and type(target) is str and worktrees:
            return _under(target, worktrees)
        return True
    if name == "Bash":
        return bash_is_work(str(tool_input.get("command", "")))
    if name.startswith("mcp__"):
        return mcp_is_work(name)
    return False


IDLE_FROM_RE = re.compile(r'"type"\s*:\s*"idle_notification".*?"from"\s*:\s*"([^"]+)"', re.S)


def _idle_name(raw: str) -> str:
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", raw)  # a display suffix like "worker [3fa9c1]"


class _Scan:
    """One transcript's signals: newest work (at or before ``until``), idle notifications by name, tool results by id."""

    def __init__(self) -> None:
        self.latest: datetime | None = None
        self.idle: dict[str, datetime] = {}
        self.tool_results: dict[str, datetime] = {}


def _scan_lines(transcript: Path, worktrees: list[Path], *, until: datetime | None = None) -> _Scan:
    scan = _Scan()
    with transcript.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"tool_use"' not in line and "idle_notification" not in line and '"tool_result"' not in line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if type(raw) is not dict:
                continue
            when = _parse_ts(str(raw.get("timestamp", "")))
            if when is None:
                continue
            message = raw.get("message")
            content = message.get("content") if type(message) is dict else None
            if raw.get("type") == "user":
                if "idle_notification" in line:
                    found = IDLE_FROM_RE.search(content if type(content) is str else json.dumps(content))
                    if found:
                        name = _idle_name(found.group(1))
                        if name not in scan.idle or when > scan.idle[name]:
                            scan.idle[name] = when
                if type(content) is list:
                    for block in content:
                        if type(block) is dict and block.get("type") == "tool_result" and block.get("tool_use_id"):
                            scan.tool_results[str(block["tool_use_id"])] = when
                continue
            if raw.get("type") != "assistant" or type(content) is not list:
                continue
            if until is not None and when > until:
                continue
            for block in content:
                if type(block) is not dict or block.get("type") != "tool_use":
                    continue
                tool_input = block.get("input") if type(block.get("input")) is dict else {}
                if tool_is_work(str(block.get("name", "")), tool_input, worktrees) and (scan.latest is None or when > scan.latest):
                    scan.latest = when
    return scan


def _subagent_meta(transcript: Path) -> dict[str, object]:
    meta = transcript.with_suffix(".meta.json")
    if not meta.is_file():
        return {}
    try:
        loaded = json.loads(meta.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return loaded if type(loaded) is dict else {}


def _handed_back(meta: dict[str, object], parent: _Scan) -> tuple[bool, datetime | None]:
    """(hand-back channel known, hand-back time). A foreground Agent-tool subagent hands back through the parent's
    tool_result for its spawning call (``toolUseId``); a teammate through an idle notification by name."""
    tool_use_id = meta.get("toolUseId")
    if type(tool_use_id) is str and tool_use_id:
        return True, parent.tool_results.get(tool_use_id)
    name = meta.get("name")
    if (type(name) is str and name) or meta.get("taskKind") == "in_process_teammate":
        return True, parent.idle.get(_idle_name(str(name or "")))
    return False, None


def _repo_worktrees(repo: Path) -> list[Path]:
    proc = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"], capture_output=True, text=True, check=False)
    roots = [Path(line.split(" ", 1)[1]).resolve() for line in proc.stdout.splitlines() if line.startswith("worktree ")]
    return roots or [repo.resolve()]


def scan_transcript(transcript: Path, repo: Path) -> datetime | None:
    """Newest work-signal timestamp across the session and its subagents; None if the transcript is unreadable or quiet.

    A subagent's signals count once its work is HANDED BACK — the parent's tool_result for a foreground Agent call,
    or an idle notification for a teammate — and only up to that time: work still in a running subagent's hands
    has not been handed back, so nothing can have been claimed on it, and a running subagent must not re-arm the
    gate on every yield the parent makes while waiting. A subagent whose metadata names no hand-back channel is
    counted in full (fail closed). One killed before handing back is invisible: the documented fail-open.
    """
    if not transcript.is_file():
        return None
    worktrees = _repo_worktrees(repo)
    parent = _scan_lines(transcript, worktrees)
    latest = parent.latest
    for sub in sorted((transcript.parent / transcript.stem / "subagents").glob("agent-*.jsonl")):
        known, handed_back = _handed_back(_subagent_meta(sub), parent)
        if known and handed_back is None:
            continue  # still running
        sub_latest = _scan_lines(sub, worktrees, until=handed_back).latest
        if sub_latest is not None and (latest is None or sub_latest > latest):
            latest = sub_latest
    return latest


def _repo_for(cwd: str) -> Path:
    proc = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        return Path(proc.stdout.strip())
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or cwd)


def _bump_blocks(repo: Path, session_id: str, release_marker: str) -> int:
    """Blocks since the session's last release (PASS or withdrawal) — NOT per work signal, so more work cannot re-arm it."""
    path = repo / ".verify" / ".hook" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, object] = {"since": release_marker, "blocks": 0}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if type(loaded) is dict and loaded.get("since") == release_marker:
                state = loaded
        except json.JSONDecodeError:
            pass
    state["blocks"] = int(str(state.get("blocks", 0))) + 1
    path.write_text(json.dumps(state), encoding="utf-8")
    return int(str(state["blocks"]))


def decide(payload: dict[str, object]) -> dict[str, object]:
    pi = _load_prove_it()
    session_id = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or os.getcwd())
    repo = _repo_for(cwd)
    work = scan_transcript(Path(str(payload.get("transcript_path") or "")), repo)
    if work is None or not session_id:
        return {}
    script = f"python {HERE / 'prove_it.py'}"
    claims = pi.claims_for_session(repo, session_id)
    verdict = pi.latest_verdict(repo, session_id=session_id)
    released: list[tuple[datetime, str]] = []
    if verdict is not None and verdict.verdict == pi.VERDICT_PASS:
        released.append((pi.parse_iso(verdict.checked_at), f"PASS verdict {verdict.path}"))
    for claim in claims:
        if claim.withdrawn:
            released.append((pi.parse_iso(str(claim.withdrawn["at"])), f"claim {claim.claim_id} withdrawn: {claim.withdrawn['reason']}"))
    newest = max(released, default=None)
    if newest is not None and newest[0] >= work:
        message = "prove-it: " + newest[1]
        if "withdrawn" in newest[1]:
            message += " — the work is NOT verified; report it as incomplete and name what could not be substantiated."
        return {"systemMessage": message}

    open_claims = [c for c in claims if not c.withdrawn and pi.parse_iso(c.created_at) >= work]
    if not open_claims:
        reason = (
            f"prove-it: this session changed files or history (last signal {work.isoformat(timespec='seconds')}) and has no claim covering it. "
            "Do not tell the user the work is complete. Decompose the completion claim into falsifiable assertions and file it: "
            f"`{script} claim --claim '<one sentence>' --assert 'test: <cmd>' --assert 'commit: <sha> on <branch>' "
            "--assert 'mutation: <pytest cmd> :: <fix paths> [@ <base>]' ...`, "
            f"then `{script} verify --claim <id>`, then spawn the adversarial reviewer and record "
            f"`{script} review --claim <id> --verdict PASS|FAIL --findings <file>`. "
            f"To stop without claiming success (waiting, blocked, or honestly not done): `{script} withdraw --reason '<why this work is not being claimed>'`, "
            f"or for a filed claim `{script} withdraw --claim <id> --reason '<what could not be substantiated>'`."
        )
    else:
        claim = open_claims[-1]
        current = verdict if verdict is not None and verdict.claim_id == claim.claim_id else None
        if current is None:
            reason = f"prove-it: claim {claim.claim_id} has not been verified. Run `{script} verify --claim {claim.claim_id}` (it writes .verify/<timestamp>.md)."
        elif current.verdict == pi.VERDICT_UNREVIEWED:
            reason = (
                f"prove-it: claim {claim.claim_id} passed the deterministic checks but the adversarial review is not recorded. "
                "Spawn the reviewer subagent (goal: falsify every assertion from a fresh worktree), then "
                f"`{script} review --claim {claim.claim_id} --verdict PASS|FAIL --findings <file>`."
            )
        else:  # FAIL — a PASS on a claim newer than the work would have released above, so nothing else reaches here
            reason = (
                f"prove-it: verdict {current.verdict} for claim {claim.claim_id} — unproven: {', '.join(current.unproven) or 'reviewer rejected'} (see {current.path}). "
                f"Either fix and re-run `{script} verify --claim {claim.claim_id}`, or `{script} withdraw --claim {claim.claim_id} --reason '<why>'` "
                "and report exactly which claims could not be substantiated. Never report success."
            )
    blocks = _bump_blocks(repo, session_id, newest[0].isoformat() if newest is not None else "none")
    if blocks > pi.MAX_BLOCKS:
        return {
            "systemMessage": f"prove-it: released after {pi.MAX_BLOCKS} blocks without a PASS verdict. The work is NOT verified — say so to the user. Last reason: {reason}"
        }
    return {"decision": "block", "reason": reason}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if type(payload) is not dict:
            payload = {}
        result = decide(payload)
    except Exception as exc:  # a broken gate must not trap every session; say so instead
        result = {"systemMessage": f"prove-it stop hook error (not enforcing): {exc.__class__.__name__}: {exc}"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
