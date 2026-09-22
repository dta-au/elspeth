# Migrating ELSPETH's issue tracking to GitHub Issues

ELSPETH tracked its work in **filigree**, an agent-native issue tracker with a local
database. That fitted a project with one developer and a fleet of agents. It does not fit
a project with several developers, so GitHub Issues becomes the system of record.

This directory is the migration: one Markdown file per issue, reviewed before anything is
published, plus the tooling that checks and imports them.

```
STYLE.md            the house style and the redaction rules — read this first
check_issues.py     the pre-publication gate; exits non-zero on anything unsafe
issues/             one file per issue, <slug>.md
```

## Why a staging directory instead of importing straight from the tracker

Three reasons, in order of how much they would have cost.

**The source is not publishable.** The tracker was written for an audience of one, inside
a private tool. A scan of the 48 tickets in scope found a cloud account identifier, an
internal deployment hostname, a client organisation's name, session identifiers, agent and
worktree names, and user home paths. Every one of those would have gone public verbatim on
a direct import. `check_issues.py` encodes exactly the patterns that were actually found,
and it fails the build rather than warning.

**The audience changed.** A tracker entry is a note to yourself. A GitHub issue is a
briefing for someone who has never opened the codebase, cannot see the old tracker, and
cannot ask the person who filed it. Those are different documents, and the second one has
to be written, not exported.

**Some of the source was wrong.** Tickets accumulated corrections, and several had their
founding premise retracted in their own comments while the title still asserted it. A
staging pass is where that gets caught. See `STYLE.md` § Honesty rules.

## The pre-publication gate

```bash
python3 docs/github-issues/check_issues.py
```

Read-only. Reports two classes:

- **BLOCK** — must never be published: infrastructure identifiers, internal hostnames,
  client names, session identifiers, agent and lane names, user home paths,
  credential-shaped strings, internal tracker ids, missing front matter.
- **warn** — house-style drift: internal jargon, shouting, US spelling, status claims,
  a missing `## Fix` section, suspicious length.

A non-zero exit means **not safe to publish**. Never weaken a pattern to turn a run green;
fix the file, or add the pattern if a new class of leak is found.

## What is not migrating

Two categories stay out of GitHub, both by operator ruling on 2026-09-23.

**Internal governance** — the trust-tier allowlist burn-downs, judge-signing tooling, the
lint gate's own internals, and the agent tooling. These are the project's own machinery
rather than product defects, and they mean nothing to an outside contributor. They keep
the `exclude:gh-migration` label in filigree.

**Work awaiting a decision** — items held pending an operator call, labelled
`wait:decision`. They migrate once decided, not before.

## Container work

GitHub has no `milestone` / `phase` / `step` issue type, so two multi-part programmes did
not survive as issue hierarchies. Each became one issue pointing at a folder:

| Programme | Detail |
|---|---|
| Identity and workflow governance | `docs/programmes/identity-workflow-governance/` |
| State engine — completion to 1.0 | `docs/programmes/state-engine-1.0/` |

Each folder holds a README with the full scope and a `tracker-rows.json` preserving the
original rows verbatim. Those folders were written and committed **before** the tracker
rows were closed, so no detail depends on the old tracker to survive.

## Provenance

The triage that produced this set — what was closed as already-done, what was consolidated,
and the evidence for each — is `docs/reviews/2026-09-22-p1-triage.md`. It records 76 of 134
open P1 items closed before migration, with the measuring command beside each claim.
