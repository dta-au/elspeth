#!/usr/bin/env python3
"""Pre-publication gate for the GitHub issue set.

These files go to a PUBLIC repository. This script is the check that decides whether
that is safe. It is read-only and exits non-zero on any BLOCK.

    python3 docs/github-issues/check_issues.py

Two classes of finding:

  BLOCK  — must never be published (infrastructure ids, internal hostnames, client
           names, session ids, agent/lane names, user home paths, credentials).
  WARN   — house-style drift worth a look, not a publication risk.

Every pattern here was derived from material actually found in the source tracker on
2026-09-23, not guessed. Add to it when a new class of leak is found; never weaken it
to make a run green.
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent
ISSUES = ROOT / "issues"

# --- BLOCK: never publishable -------------------------------------------------------
# Each entry: (label, compiled pattern, optional allow-predicate on the match)
BLOCK: list[tuple[str, re.Pattern[str]]] = [
    ("cloud account id (12 consecutive digits)", re.compile(r"(?<!\d)\d{12}(?!\d)")),
    (
        "internal hostname or deployment URL",
        re.compile(r"https?://(?!(?:www\.)?github\.com|localhost|127\.0\.0\.1|example\.(?:com|org|invalid))[\w.-]+"),
    ),
    ("client / organisation name", re.compile(r"\b(?:dta[-_]?(?:dev|au|user)|foundryside)\b", re.I)),
    ("session UUID", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
    # `claude-*` is ambiguous: an agent identity (claude-ticket-closeout,
    # claude-fable-8673059d) must never be published, but a MODEL name
    # (anthropic/claude-sonnet-5) is legitimate content in a ticket about which
    # model authored something. Provider-qualified names and the
    # <family>-<major> model forms are therefore exempt; everything else is not.
    (
        "agent / lane / worktree name",
        re.compile(
            r"(?<![\w/-])(?:"
            r"codex-[\w-]+"
            r"|(?<!/)claude-(?!(?:sonnet|opus|haiku)-\d)[\w.-]+"
            r"|lane-\d+|lane/[\w-]+|preflight-verify|team-lead"
            r")\b"
        ),
    ),
    ("user home path", re.compile(r"/(?:home|Users)/[A-Za-z_][\w-]*")),
    (
        "credential-shaped assignment",
        re.compile(r"\b(?:api[_-]?key|password|passwd|secret|bearer|access[_-]?token)\s*[=:]\s*[\"']?[\w./+-]{6,}", re.I),
    ),
    ("internal tracker id", re.compile(r"\belspeth-[0-9a-f]{10}\b")),
]

# --- WARN: house-style drift --------------------------------------------------------
WARN: list[tuple[str, re.Pattern[str]]] = [
    ("internal jargon", re.compile(r"\b(?:the ruling|operator ruling|this lane|the hub|seat)\b", re.I)),
    ("shouty emphasis (4+ caps run)", re.compile(r"(?<![\w`/-])[A-Z]{4,}(?![\w`-])")),
    ("US spelling", re.compile(r"\b\w*(?:behavior|serializ|normaliz|initializ|authoriz)\w*\b", re.I)),
    ("claims a status", re.compile(r"\b(?:is (?:now )?fixed|has landed|was released|shipped in)\b", re.I)),
]

# Words that legitimately appear in caps and are not shouting.
CAPS_OK = {
    # Extra technical acronyms that are not shouting.
    "ASGI",
    "WSGI",
    "ORM",
    "CRUD",
    "JWT",
    "RBAC",
    "SSE",
    "TTL",
    "CPU",
    "RAM",
    "GPU",
    "ARN",
    "IAM",
    "ECS",
    "EKS",
    "AKS",
    "SQS",
    "SNS",
    "RDS",
    "VPC",
    "DNS",
    "CORS",
    "CSRF",
    "XSS",
    "SAST",
    "DAST",
    "SBOM",
    "OWASP",
    "WCAG",
    "RFC",
    "ISO",
    "UTF",
    "ASCII",
    "REST",
    "GRPC",
    "STDOUT",
    "STDERR",
    "STDIN",
    "PostgreSQL",
    "SQLite",
    "ADR",
    "AGENTS",
    "API",
    "AWS",
    "CHECK",
    "CI",
    "CLI",
    "CSV",
    "DAG",
    "DTO",
    "MIME",
    "SQLSTATE",
    "ELSPETH",
    "FAIL",
    "GET",
    "HEAD",
    "HMAC",
    "HTML",
    "HTTP",
    "HTTPS",
    "JSON",
    "LLM",
    "NOTE",
    "PASS",
    "PDF",
    "PNG",
    "POST",
    "README",
    "SDK",
    "SQL",
    "TODO",
    "URL",
    "UTC",
    "UUID",
    "WARN",
    "YAML",
    "PRD",
    "MCP",
    "OIDC",
    "SSO",
    "TLS",
    "RSS",
    "CVE",
    "OTel",
    "PR",
}

REQUIRED_FRONT_MATTER = ("title", "labels")


def scan(path: pathlib.Path) -> tuple[list[str], list[str]]:
    text = path.read_text(encoding="utf-8")
    blocks: list[str] = []
    warns: list[str] = []

    # BLOCK patterns scan EVERYTHING, fenced code included: a leaked account id inside a
    # code block is still a leaked account id.
    for label, pattern in BLOCK:
        for m in dict.fromkeys(pattern.findall(text)):
            blocks.append(f"{label}: {m!r}")

    # WARN patterns are about prose style, so they skip fenced code and inline spans.
    # Capitals inside a code block are data — a constant name, a column header, a literal
    # enum value — not shouting, and flagging them trains the reader to ignore the gate.
    prose = re.sub(r"```.*?```", "", text, flags=re.S)
    prose = re.sub(r"`[^`\n]*`", "", prose)

    for label, pattern in WARN:
        for m in dict.fromkeys(pattern.findall(prose)):
            if label.startswith("shouty") and m in CAPS_OK:
                continue
            warns.append(f"{label}: {m!r}")

    # Front matter is EXECUTABLE, not decoration. A regex sees `title: Repair X: Y` as
    # fine; YAML sees a mapping value where none is allowed and the importer dies. So
    # parse it the way the importer will, rather than pattern-matching it.
    if not text.startswith("---\n"):
        blocks.append("missing YAML front matter")
    else:
        fm = text.split("---\n", 2)[1]
        try:
            meta = yaml.safe_load(fm)
        except yaml.YAMLError as exc:
            meta = None
            first = str(exc).splitlines()[0]
            blocks.append(f"front matter is not valid YAML ({first}) — the importer would fail")
        if meta is not None and not isinstance(meta, dict):
            blocks.append(f"front matter parsed as {type(meta).__name__}, not a mapping")
            meta = None
        if isinstance(meta, dict):
            for key in REQUIRED_FRONT_MATTER:
                if key not in meta:
                    blocks.append(f"front matter missing {key!r}")
            title = meta.get("title")
            if isinstance(title, str) and len(title) > 100:
                warns.append(f"title is {len(title)} chars — over the 100 guide")
            labels = meta.get("labels") or []
            if not isinstance(labels, list):
                blocks.append("front matter 'labels' is not a list")
            elif not any(str(x).startswith("type/") for x in labels):
                warns.append("front matter has no type/* label")

    body = text.split("---\n", 2)[-1]
    words = len(body.split())
    if words < 60:
        warns.append(f"very short ({words} words) — is it startable by a new dev?")
    if words > 900:
        warns.append(f"very long ({words} words) — length must be earned")
    # Every issue needs a section saying what someone would actually DO. A bug calls that
    # "Fix"; a task more naturally calls it "Scope", and may pair it with "Verification"
    # or "Proposed shape". Any of them satisfies the requirement — the point is that a new
    # developer can tell what done looks like, not that a particular word appears.
    ACTIONABLE = (
        "## Fix",
        "## What is needed",
        "## Scope",
        "## Proposed",
        "## Verification",
        "## What to do",
        "## Acceptance",
        "## Remediation",
        "## Resolution",
    )
    if not any(h in body for h in ACTIONABLE):
        warns.append("no actionable section (Fix / Scope / Proposed / Verification) — a new developer cannot tell what done looks like")

    return blocks, warns


def main() -> int:
    if not ISSUES.is_dir():
        print(f"no issues directory at {ISSUES}", file=sys.stderr)
        return 2
    files = sorted(ISSUES.glob("*.md"))
    if not files:
        print(f"no .md files in {ISSUES}", file=sys.stderr)
        return 2

    total_block = total_warn = 0
    for f in files:
        blocks, warns = scan(f)
        total_block += len(blocks)
        total_warn += len(warns)
        if blocks or warns:
            print(f"\n{f.relative_to(ROOT.parent.parent)}")
            for b in blocks:
                print(f"   BLOCK  {b}")
            for w in warns:
                print(f"   warn   {w}")

    print(f"\n{'=' * 66}")
    print(f"{len(files)} files | {total_block} BLOCK | {total_warn} warn")
    if total_block:
        print("NOT SAFE TO PUBLISH — resolve every BLOCK first.")
    else:
        print("No publication blockers found.")
    return 1 if total_block else 0


if __name__ == "__main__":
    raise SystemExit(main())
