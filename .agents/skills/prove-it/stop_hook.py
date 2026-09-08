#!/usr/bin/env python3
"""Claude Code Stop hook: refuse to let a session that did work stop without a prove-it PASS.

Reads the hook payload on stdin (session_id, transcript_path, cwd), scans the session's
transcript for WORK SIGNALS (Edit/Write/NotebookEdit tool uses; Bash commands that write files
or commit/merge), and compares the newest one against the session's newest prove-it release:
a ``PASS`` verdict or an explicit, reasoned withdrawal. Work newer than the last release blocks
the stop with a reason that names the exact next command. Other sessions' claims are invisible.

After MAX_BLOCKS consecutive blocks for the same work the hook releases the session with a loud
systemMessage — a gate that can loop forever is a denial of service, not a control — and says
plainly that the work is NOT verified.

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
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
WORK_RE = re.compile(
    r"\bgit\s+(commit|merge|cherry-pick|rebase|am|apply|revert)\b|lane_manager\.py\s+merge\b|\bcat\s*>|\btee\b|\bsed\s+-i\b"
    r"|>\s*[\w./-]+\.(py|md|json|toml|yaml|yml|ts|tsx|js|sh)\b"
)
SELF_RE = re.compile(r"prove_it\.py|stop_hook\.py")
COMPLETION_RE = re.compile(
    r"\b(complete[ds]?|done|fixed|finished|pass(es|ed|ing)?|green|landed|merged|committed|resolved|implemented|verified|works|shipped"
    r"|ready (for|to) (review|merge|ship)|no longer reproduces|all set)\b",
    re.IGNORECASE,
)


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


def scan_transcript(transcript: Path) -> tuple[datetime | None, str]:
    """(newest work-signal timestamp, the assistant's final text) — (None, "") if the transcript is unreadable or quiet.

    A work signal is a tool use that changed files or history. The final text is what the user is about to read;
    the hook only enforces when it reads as a completion claim, so a session can yield to wait without claiming.
    """
    if not transcript.is_file():
        return None, ""
    latest: datetime | None = None
    last_text = ""
    with transcript.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"tool_use"' not in line and '"text"' not in line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if type(raw) is not dict or raw.get("type") != "assistant":
                continue
            message = raw.get("message")
            content = message.get("content") if type(message) is dict else None
            if type(content) is not list:
                continue
            texts = [
                str(b.get("text", "")) for b in content if type(b) is dict and b.get("type") == "text" and str(b.get("text", "")).strip()
            ]
            if texts:
                last_text = "\n".join(texts)
            when = _parse_ts(str(raw.get("timestamp", "")))
            if when is None:
                continue
            for block in content:
                if type(block) is not dict or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name", ""))
                tool_input = block.get("input") if type(block.get("input")) is dict else {}
                is_work = name in EDIT_TOOLS
                if name == "Bash":
                    command = str(tool_input.get("command", ""))
                    is_work = bool(WORK_RE.search(command)) and not SELF_RE.search(command)
                if is_work and (latest is None or when > latest):
                    latest = when
    return latest, last_text


def _repo_for(cwd: str) -> Path:
    proc = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        return Path(proc.stdout.strip())
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or cwd)


def _bump_blocks(repo: Path, session_id: str, work_ts: str) -> int:
    path = repo / ".verify" / ".hook" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, object] = {"work": work_ts, "blocks": 0}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if type(loaded) is dict and loaded.get("work") == work_ts:
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
    work, last_text = scan_transcript(Path(str(payload.get("transcript_path") or "")))
    if work is None or not session_id:
        return {}
    if not COMPLETION_RE.search(last_text):
        return {}  # the user is not being told anything is complete; yielding to wait is not a claim
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
            "--assert 'mutation: <test cmd> :: <fix paths> [@ <base>]' ...`, "
            f"then `{script} verify --claim <id>`, then spawn the adversarial reviewer and record "
            f"`{script} review --claim <id> --verdict PASS|FAIL --findings <file>`. "
            f"To stop without claiming success: `{script} withdraw --claim <id> --reason '<what could not be substantiated>'`."
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
        elif current.verdict == pi.VERDICT_FAIL:
            reason = (
                f"prove-it: verdict FAIL for claim {claim.claim_id} — unproven: {', '.join(current.unproven) or 'reviewer rejected'} (see {current.path}). "
                f"Either fix and re-run `{script} verify --claim {claim.claim_id}`, or `{script} withdraw --claim {claim.claim_id} --reason '<why>'` "
                "and report exactly which claims could not be substantiated. Never report success."
            )
        else:
            reason = f"prove-it: work newer than the PASS verdict at {current.checked_at}. File a new claim for the new work, verify it, and record the review."
    blocks = _bump_blocks(repo, session_id, work.isoformat())
    if blocks > pi.MAX_BLOCKS:
        return {
            "systemMessage": f"prove-it: released after {blocks - 1} blocks without a PASS verdict. The work is NOT verified — say so to the user. Last reason: {reason}"
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
