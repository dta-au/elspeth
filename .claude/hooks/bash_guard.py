"""PreToolUse guard for the Bash tool.

Reads the hook JSON on stdin and refuses (exit 2, reason on stderr) commands
that agents have run by mistake with wide damage:

- ``git commit --amend`` and ``git checkout -- <path>``;
- broad or pattern-based process kills. An agent that meant to stop its own
  run has more than once stopped every other session's processes instead.

A kill of explicit numeric PIDs (``kill -TERM 12345``) is allowed: that is the
form the integration book's kill procedure uses once the lead integrator has
authorised it.

Only what the shell would run is checked. Quoted text, heredoc bodies and
comments are removed first, so a commit message, a document or a grep that
mentions these commands is not refused. The exception is ``python -c``, whose
quoted code is checked for ``killpg`` and ``os.kill`` on a group or on every
process. This is a backstop for slips: it does not see Codex, scripts that
stop their own children, or commands typed by hand.
"""

from __future__ import annotations

import json
import re
import sys

BOOK = 'docs/maintainer/integration-book.md, section 8, "How to kill a process"'

KILL_MESSAGE = (
    "broad or pattern-based kill (pkill, killall, kill -1, kill 0, kill -- -PGID, "
    "kill $(...), xargs kill, killpg). All process kills, CI cancels included, go "
    "via the lead integrator: send it the exact PIDs with each one's command line, "
    "cwd, parent and age, and after its explicit authority signal those PIDs by "
    f"number (kill -TERM <pid>). Procedure: {BOOK}. If this refusal is wrong (the "
    "command is not a kill), tell the lead integrator so the guard can be fixed."
)

# A word in command position: line start, or after ; & | ( { or a backtick,
# optionally behind a wrapper (sudo, nohup, exec, command, env with its
# settings, time, nice, ionice, setsid, stdbuf, timeout with its duration) or a
# loop or if keyword. The command word may carry a path (/usr/bin/pkill).
#
# An option's argument may not itself start with "-": otherwise "-u -u -u ..."
# can be split two ways at every step and matching time grows exponentially
# (measured: 28 repeats took 2 s). A hook that times out lets the command run.
_TOK = r"[^\s;&|)]+"
_ARG = rf"(?!-){_TOK}"
_WRAP = (
    r"(?:(?:nohup|exec|command|time|setsid|do|then|else)\s+"
    # Option loops are atomic and possessive (Python 3.11+): each option is
    # read one way only, so a word such as "sudo" after "-u" cannot be tried
    # both as the option's value and as a new wrapper.
    rf"|sudo(?>\s+(?:-[ugCDhpRTUr]\s+{_ARG}|-{_TOK}))*+\s+"
    rf"|(?:env|nice|ionice|stdbuf)(?>\s+(?:-{_TOK}|\w+=\S*|\d+))*+\s+"
    rf"|timeout(?>\s+(?:-[ks]\s+{_ARG}|-{_TOK}))*+\s+\S+\s+)"
)
_CP = rf"(?:^|[;&|({{`])\s*{_WRAP}*(?:[^\s;&|()]*/)?"
_OPT = rf"(?:-[sn]\s+{_ARG}|-{_TOK})"
_END = r"(?=\s|$|[;&|)])"

KILL_SHAPES = [
    re.compile(rf"{_CP}(?:pkill|killall){_END}", re.M),
    re.compile(rf"{_CP}kill(?:\s+{_OPT})+\s+(?:--\s+)?-\d+{_END}", re.M),
    re.compile(rf"{_CP}kill\s+--\s+-\d+{_END}", re.M),
    re.compile(rf"{_CP}kill(?:\s+{_TOK})*\s+0{_END}", re.M),
    re.compile(rf"{_CP}kill(?:\s+{_TOK})*\s+[\"']?(?:\$\(|`)", re.M),
    re.compile(
        rf"{_CP}xargs(?:\s+-{_TOK})*\s+{_WRAP}*(?:[^\s;&|()]*/)?(?:kill|pkill|killall){_END}",
        re.M,
    ),
]
# A shell started with -c, or eval, runs its quoted argument: check it too.
SHELL_C = re.compile(
    rf"{_CP}(?:(?:ba|da|z|k)?sh(?:\s+-[a-zA-Z]+)*\s+-[a-zA-Z]*c[a-zA-Z]*|eval)\s+(['\"])([^'\"]*)\1",
    re.M,
)
# A quoted word the shell runs as the command, or passes to kill as an
# argument, is unquoted before matching ("pkill" x, kill "-1").
QUOTED_COMMAND_WORD = re.compile(rf"({_CP})(['\"])(_+)\2", re.M)
QUOTED_KILL_ARG = re.compile(rf"({_CP}kill(?:\s+[^\s;&|)]+)*?\s+)(['\"])(_+)\2", re.M)
# Only a plain word is put back. A quoted word holding spaces or shell
# punctuation is one word to the shell ("pkill x" names no real command), and
# putting its separators back would show the patterns commands that do not run.
PLAIN_WORD = re.compile(r"[A-Za-z0-9_./=+:@%,-]+")


def _restore_live_words(live: str, original: str) -> str:
    """Put back quoted words that sit where a command or a kill argument goes."""
    for pattern, strip in ((QUOTED_COMMAND_WORD, False), (QUOTED_KILL_ARG, True)):
        pos = 0
        while True:
            match = pattern.search(live, pos)
            if match is None:
                break
            start, end = match.start(3), match.end(3)
            word = original[start:end]
            pos = end + 1
            # kill reads a padded number (" -1") as the number; a command name
            # with padding names no command, so only kill arguments are stripped.
            if PLAIN_WORD.fullmatch(word.strip() if strip else word) is None:
                continue
            # Keep offsets: the quote marks and any padding become spaces.
            live = live[: start - 1] + " " + re.sub(r"\s", " ", word) + " " + live[end + 1 :]
    return live


GIT_SHAPES = [
    re.compile(rf"{_CP}git\s+(?:-\S+\s+)*commit\b[^\n;&|]*\s--amend\b", re.M),
    # Both `git checkout -- <path>` and `git checkout <ref> -- <path>`: each
    # overwrites uncommitted work in those paths, which has lost work before.
    re.compile(rf"{_CP}git\s+(?:-\S+\s+)*checkout\b[^\n;&|]*\s--(?:\s|$)", re.M),
]
PYTHON_C = re.compile(r"\bpython[0-9.]*\s+(?:-\S+\s+)*-c\b")
PYTHON_KILL = re.compile(r"\bkillpg\s*\(|\bos\.kill\(\s*(?:-|0\s*[,)])")
HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def _split_heredocs(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return ``text`` without heredoc bodies, and each (opening line, body)."""
    out: list[str] = []
    bodies: list[tuple[str, str]] = []
    pending: list[tuple[bool, str, str]] = []
    body: list[str] = []
    for line in text.split("\n"):
        if pending:
            strip_tabs, word, opener = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == word:
                bodies.append((opener, "\n".join(body)))
                body = []
                pending.pop(0)
            else:
                body.append(line)
            continue
        out.append(line)
        pending.extend((m.group(1) == "-", m.group(3), line) for m in HEREDOC.finditer(line))
    if pending and body:
        bodies.append((pending[0][2], "\n".join(body)))
    return "\n".join(out), bodies


# A heredoc whose opening line runs a shell or Python feeds it code to run.
SHELL_FED = re.compile(rf"{_CP}(?:ba|da|z|k)?sh\b[^;&|\n]*<<", re.M)
PYTHON_FED = re.compile(rf"{_CP}python[0-9.]*\b[^;&|\n]*<<", re.M)


def _drop_quotes_and_comments(text: str) -> str:
    """Blank quoted text and unquoted comments, keeping every offset.

    Quoted characters become ``_`` and comment characters become spaces, so a
    match in the result maps to the same span of ``text``. Inside double quotes
    ``$(`` and backticks are kept: the shell still runs those substitutions.
    """
    out: list[str] = []
    i, n = 0, len(text)
    quote = ""
    while i < n:
        ch = text[i]
        if quote == "'":
            out.append(ch if ch == "'" else ("\n" if ch == "\n" else "_"))
            quote = "" if ch == "'" else quote
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                out.append("__")
                i += 2
                continue
            if ch == '"':
                quote = ""
                out.append(ch)
            elif text.startswith("$(", i):
                out.append("$(")
                i += 2
                continue
            elif ch == "`":
                out.append(ch)
            else:
                out.append("\n" if ch == "\n" else "_")
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(text[i : i + 2])
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def refusal(command: str, depth: int = 0) -> str | None:
    """Return the reason to refuse ``command``, or None to let it run."""
    without_heredocs, bodies = _split_heredocs(command)
    live = _restore_live_words(_drop_quotes_and_comments(without_heredocs), without_heredocs)
    for opener, body in bodies:
        opener_live = _drop_quotes_and_comments(opener)
        if PYTHON_FED.search(opener_live) and PYTHON_KILL.search(body):
            return KILL_MESSAGE
        if depth < 3 and SHELL_FED.search(opener_live):
            inner = refusal(body, depth + 1)
            if inner is not None:
                return inner
    if depth < 3:
        # Found in the blanked text, so only a shell actually being run counts;
        # its argument is then read back from the original at the same offsets.
        for match in SHELL_C.finditer(live):
            inner = refusal(without_heredocs[match.start(2) : match.end(2)], depth + 1)
            if inner is not None:
                return inner
    for shape in GIT_SHAPES:
        if shape.search(live):
            return (
                "git commit --amend and git checkout [<ref>] -- <path> are refused here: "
                "the first rewrites a commit, the second overwrites uncommitted work in "
                "those paths. If you need one, ask the lead integrator."
            )
    for shape in KILL_SHAPES:
        if shape.search(live):
            return KILL_MESSAGE
    if PYTHON_C.search(live) and PYTHON_KILL.search(command):
        return KILL_MESSAGE
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command:
        return 0
    reason = refusal(command)
    if reason is None:
        return 0
    sys.stderr.write(f"BLOCKED by .claude/hooks/bash_guard.py: {reason}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
