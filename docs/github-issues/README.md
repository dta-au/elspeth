# Migrating ELSPETH's issue tracking to GitHub Issues

As of 2026-09-25, GitHub Issues is the shared system of record and the local
tracker integrations are removed. The maintainer will triage and upload the
remaining archived backlog separately; this retirement does not publish issues.

ELSPETH previously tracked its work in a local issue database. That fitted a project with one developer and a fleet of agents. It does not fit
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

**Internal governance** — the trust-tier allowlist burn-downs, the lint gate's own
internals, and the agent tooling. These are the project's own machinery rather than
product defects, and they mean nothing to an outside contributor. They remain in the private archive with their
`exclude:gh-migration` labels until the maintainer decides their disposition.

> **A security defect is not internal governance, even when it lives in governance
> tooling.** An earlier draft of this file excluded "judge-signing tooling" wholesale,
> which would have put the CI key-exposure findings — the work that makes this repository
> safe to give several people push access to — in a local database those same people
> cannot read. That is the opposite of what the migration is for, and it would have meant
> the local tracker was not actually being retired.
>
> The exclusion covers *ceremony*: burn-downs, allowlist hygiene, the signing workflow
> itself. It does not cover a vulnerability. An unfixed vulnerability belongs in a
> **private GitHub security advisory**, where the developers who must fix it can see it
> and the world cannot, and becomes a public issue once it is fixed. See
> `docs/reviews/2026-09-23-single-developer-assumptions.md` F-01–F-04 and F-41.

**Work awaiting a decision** — items held pending an operator call, labelled
`wait:decision`. They migrate once decided, not before.

## Container work

GitHub has no `milestone` / `phase` / `step` issue type, so two multi-part programmes did
not survive as issue hierarchies. Each became one issue pointing at a folder:

| Programme | Detail |
|---|---|
| Identity and workflow governance | `docs-archive/2026-09-23-identity-workflow-governance/` |
| State engine — completion to 1.0 | `docs/programmes/state-engine-1.0/` |

Each folder holds a README with the full scope and a `tracker-rows.json` preserving the
original rows verbatim. Those folders were written and committed **before** the tracker
rows were closed, so no detail depends on the old tracker to survive.

## The import happened — 2026-09-23

**44 issues created as `#158`–`#201`**, all authored by `johnm-dta`, all labelled.
`.import-state.jsonl` records every slug against its issue number and URL.

Before publication the whole set went through a review against one question: does
anything here reflect badly on the Australian Public Service. It returned **34 PASS,
10 FIX and 2 HOLD** — `docs/reviews/2026-09-23-aps-publication-review.md`. The ten
fixes were applied first; the two holds are in `held/` with the decision each needs.

That review established one fact worth repeating, because it changes what withholding
an issue achieves: **these files were already public.** They have been on
`origin/release/0.8.1` of this public repository since they were written. Creating a
GitHub issue adds indexing, notification, cross-linking and permanence — amplification,
not disclosure. Holding a file back withholds reach, not content, so anything whose
*text* is the concern needs the tracked file dealt with as well.

Forty tracker rows were closed with the GitHub URL in the close reason. **Four were
not**, because another session held a live claim on them; each got a comment naming
its GitHub issue and was left alone. They are `#158`, `#162`, `#188` and `#190` —
their historical claims are now retained only in the private archive.

## After the import

Two things can only be done once issues have numbers.

**Cross-references.** Four issues point at a sibling in prose — "raise a separate issue
for it", "tracked separately", "the guided retirement". They read correctly as written,
but in a system of record they want real `#123` links. `import_issues.py` writes
`.import-state.jsonl` mapping every slug to its issue number, which is the input for that
pass. It is deliberately manual: the references are prose, not slugs, and a regex that
guessed at them would produce confident wrong links.

**The held set.** `held/` contains issues whose work verification found already in the
tree. Each needs a decision — publish the remaining scope, or close the tracker row — and
neither should be imported as written. See `held/README.md`.

## Provenance

The triage that produced this set — what was closed as already-done, what was consolidated,
and the evidence for each — is `docs/reviews/2026-09-22-p1-triage.md`. It records 76 of 134
open P1 items closed before migration, with the measuring command beside each claim.
