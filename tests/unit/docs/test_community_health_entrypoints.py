"""Regression checks for public community-health and disclosure entrypoints."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _local_link_targets(path: str) -> set[str]:
    targets: set[str] = set()
    for destination in MARKDOWN_LINK_RE.findall(_read(path)):
        target = destination.split("#", maxsplit=1)[0]
        if target and "://" not in target and not target.startswith("mailto:"):
            targets.add(target)
    return targets


def _markdown_section(text: str, heading: str) -> str:
    section_start = text.index(f"## {heading}\n")
    next_heading = text.find("\n## ", section_start + 1)
    return text[section_start : next_heading if next_heading != -1 else None]


def test_required_community_health_files_exist() -> None:
    for relative_path in ("SECURITY.md", "SUPPORT.md", "GOVERNANCE.md", "CODE_OF_CONDUCT.md"):
        assert (REPO_ROOT / relative_path).is_file(), relative_path


def test_root_entrypoints_link_to_community_health_files() -> None:
    assert "SECURITY.md" in _local_link_targets("CONTRIBUTING.md")
    assert {
        "SECURITY.md",
        "SUPPORT.md",
        "GOVERNANCE.md",
        "CODE_OF_CONDUCT.md",
    } <= _local_link_targets("README.md")


def test_security_vulnerability_guidance_uses_private_reporting_for_exploit_details() -> None:
    reporting = _markdown_section(_read("SECURITY.md"), "Reporting a Vulnerability")

    public_issue_guard = re.search(
        r"\b(?:do not|never|must not)\b[^.\n]{0,160}\bpublic\b[^.\n]{0,80}\bissue\b[^.\n]{0,160}\bexploit details\b",
        reporting,
        re.IGNORECASE,
    )
    private_route = re.search(
        r"^\d+\.\s+.*\bprivate\b.*\b(?:report|reporting|disclosure|channel|path)\b",
        reporting,
        re.IGNORECASE | re.MULTILINE,
    )

    assert public_issue_guard is not None
    assert private_route is not None
