# House style for ELSPETH GitHub issues

These files become **public GitHub issues**. They are the first thing an outside
contributor, an evaluator, or a prospective user reads about how this project handles
defects. Write them as if the reader is a competent engineer who has never seen the
codebase and is deciding whether this project is well run.

## Who this is for, and why it matters

ELSPETH is **migrating off its internal tracker onto GitHub Issues**, because it is
growing from one developer to several. These issues are not an export or an archive —
they become the system of record.

That fixes the audience: **a new developer picking one up cold**, with no access to the
old tracker and no way to ask whoever filed it. Four things follow, and they outrank
polish.

1. **It must be startable.** Name the entry point — the file, module or subsystem where
   the work begins. One sentence of orientation is usually enough: *"this lives in the
   composer's tool layer, `src/elspeth/web/composer/tools/`."*
2. **`## Fix` is the most important section, not the least.** Say what correct behaviour
   looks like and how you would know it holds. Where the source does not say, write what
   has to be decided first rather than inventing an approach.
3. **Be honest about size.** A one-line change should say so; a ticket needing a design
   decision before anyone can start should say that. Someone choosing what to pick up
   needs that signal more than they need prose.
4. **Assume no shared context.** Anything that reads as *you had to be there* — an
   unexplained acronym, a reference to a prior decision, a subsystem named but never
   located — gets half a sentence of explanation or gets cut.

One file per issue, in `issues/`, named `<slug>.md`.

## The shape

```markdown
---
title: <the issue title, sentence case, <= 100 chars>
labels: [area/composer, type/bug]
---

<One or two sentences: what is wrong, and what it costs. No preamble.>

## What happens

<The observable behaviour. What a user or operator sees.>

## Why

<The mechanism, with `file.py:line` citations. This is the part that earns trust.>

## Impact

<Who is affected and how badly. Be honest about the blast radius, including "only
under condition X" when that is the truth.>

## Fix

<What "done" looks like. Not a patch — a description of the correct behaviour and
how you would know it holds.>
```

Sections may be dropped when a ticket genuinely has nothing to put in them. Never pad.

## Redaction — non-negotiable

This repository is public. The tracker source you are given was written for an internal
audience and **contains material that must not be published**. A scan of the source found
all of the following; assume there is more.

**Remove entirely:**

| Never publish | Why | Write instead |
|---|---|---|
| Cloud account IDs (any bare 12-digit number next to `aws`, `arn:`, an account, a region) | Infrastructure identifier | "a staging AWS account" |
| Internal hostnames and deployment URLs | Attack surface | "a staging deployment" |
| Client, tenant, agency or organisation names | Not ours to disclose | "a pilot deployment" |
| Session UUIDs and short session ids (`94f6f00c`, `6990d39f`, …) | Meaningless publicly, looks like a leak | "a live session", "one reported session" |
| Agent, lane and worktree names (`codex-*`, `claude-*`, `lane/*`, `lane-01`, `preflight-verify`) | Internal process noise | Drop the attribution; state the finding |
| User home paths (`/home/<name>`, `/Users/<name>`) | Enforced by a repo gate | A repo-relative path |
| Credentials, tokens, keys in any form | Obvious | Nothing |

**Keep — these are the substance:**

- Repo-relative source paths and line numbers (`src/elspeth/web/composer/service.py:10022`)
- Commit SHAs of *this* repository
- Function, class, option and error-code names
- Measured numbers (byte budgets, counts, timings) and the command that produced them
- Test file paths and test names

## Tone

- **Lead with the defect, not the discovery story.** "Forking a session fails" beats
  "While probing an unrelated incident on 19 August, I found…".
- **Be precise about certainty.** If the source says MEASURED or REPRODUCED, say so plainly
  and give the measurement. If it was inferred, say "appears to" and do not dress it up.
  A ticket that overclaims is worse than one that admits a gap.
- **No internal doctrine, no in-jokes, no shouting.** The tracker uses ALL-CAPS emphasis,
  "ruling", "lane", "seat", "hub" and similar. None of that means anything to a reader
  outside this project. Delete it.
- **No blame and no apology.** Describe the code, not the author and not the process.
- **British/Australian spelling** to match the rest of the repository
  (behaviour, serialise, normalise).
- Keep it tight. Most issues should be 150–350 words. A genuinely complex one may run
  longer; length must be earned by content.

## Honesty rules

These matter more than polish, because a public issue that misstates the code is the thing
that actually embarrasses the project.

1. **Do not invent a citation.** If the source gives a `file:line`, use it as given. If it
   does not, describe the location in prose rather than guessing a line number.
2. **Do not upgrade a hypothesis into a finding.** Several source tickets carry explicit
   corrections and retractions — for example one whose founding premise was withdrawn in
   its own comments. If the source hedges, hedge.
3. **Do not state that something is fixed, landed or released.** Status belongs to the
   tracker, not the issue body.
4. **Carry stated limits forward.** Where the source records "this proves X but not Y",
   the issue must say so too.
5. **If the source is self-contradictory or you cannot tell what the defect is**, say so in
   a short `## Note` section rather than papering over it. Flagging an unclear ticket is a
   correct outcome, not a failure.

## Front matter

- `title` — sentence case, no trailing full stop, no internal ticket id.
- `labels` — pick from: `area/composer`, `area/engine`, `area/plugins`, `area/web`,
  `area/cli`, `area/audit`, `area/deployment`, `area/tests`, plus exactly one of
  `type/bug`, `type/task`, `type/epic`. Add `needs-triage` if the source is unclear.

Do not add any other front-matter key. The importer reads only these two.
