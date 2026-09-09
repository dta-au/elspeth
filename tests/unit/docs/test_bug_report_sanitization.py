"""Documentation must not encourage users to disclose raw composer history."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _doc_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _markdown_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    heading_level = len(heading) - len(heading.lstrip("#"))
    try:
        start = lines.index(heading)
    except ValueError as exc:
        raise AssertionError(f"missing documentation section: {heading}") from exc

    end = len(lines)
    in_code_fence = False
    for index, line in enumerate(lines[start + 1 :], start=start + 1):
        if line.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        if not line.startswith("#"):
            continue
        next_level = len(line) - len(line.lstrip("#"))
        if next_level <= heading_level:
            end = index
            break
    return " ".join("\n".join(lines[start:end]).split())


def _assert_sanitized_bug_report_guidance(section: str) -> None:
    assert re.search(r"\bsanitiz(?:e|ed|ing)\w*\s+(?:configuration|reproduction)", section, re.IGNORECASE)
    assert re.search(
        r"\b(?:do not|never)\s+attach\b.{0,100}\braw\b.{0,100}\b(?:chat history|session data|session exports?)\b",
        section,
        re.IGNORECASE,
    )
    for sensitive_term in (r"\bsecrets?\b", r"\btokens?\b", r"\bPII\b", r"\bblob contents?\b"):
        assert re.search(sensitive_term, section, re.IGNORECASE)


def test_guided_mode_bug_reports_do_not_request_raw_chat_history() -> None:
    troubleshooting = _markdown_section(
        _doc_text("docs/guides/troubleshooting.md"),
        "### Guided chat did not advance the stage",
    )

    assert "chat history attached" not in troubleshooting
    _assert_sanitized_bug_report_guidance(troubleshooting)


def test_user_manual_bug_report_pointer_requires_sanitization() -> None:
    user_manual = _markdown_section(_doc_text("docs/guides/user-manual.md"), "## Getting Help")

    _assert_sanitized_bug_report_guidance(user_manual)
