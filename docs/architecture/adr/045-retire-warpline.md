# ADR-045: Retire Warpline — Loomweave and git Answer the Change-Impact Question

**Date:** 2026-08-29
**Status:** Accepted
**Deciders:** ELSPETH maintainers
**Tags:** tooling, verification, related-adr-043, related-adr-044

## Context

Warpline is the Weft suite's temporal change-impact tool: changed entities
for a revision range, downstream impact radius, a re-verify worklist,
entity timelines and churn. It entered this repository through the same
installer sweep as Wardline ([ADR-043](043-retire-wardline.md)) and Legis
([ADR-044](044-retire-legis.md)). The open question was whether it added
value alongside Loomweave and Filigree.

Measured on 2026-08-29 with a live side-by-side probe on a production entity
changed in the last five commits (`_replace_advisor_repair_public_result`,
`src/elspeth/web/composer/service.py`):

| Question | Warpline | Source that answered it |
|---|---|---|
| Changed in `HEAD~5..HEAD` | 3 items in 2 Markdown files | git: 14 files, 10 under `src/`/`tests/` |
| Callers of the entity | `affected: []`, snapshot 2,178 commits behind | Loomweave `entity_callers_list`: 1 resolved caller (+13 candidates), matches `git grep` |
| Tests to re-run | 2 doc files, `priority: unknown`, no test names | `git grep` under `tests/`: 3 files |
| Timeline / churn | 41 file-touch events | `git log -L`: 7 commits to the function body |

The blind spot is structural. Warpline's only ingestion path is an untracked
`.git/hooks/post-commit` block, so it records only commits made directly in
the shared checkout. This project lands work from `.claude/worktrees/*`
through `--no-ff` merges; of the nine most recent commits, the six
worktree-authored or merge commits had **zero** rows in its store. Every
recorded attempt to use it says the same thing: the 2026-08-12 state-engine
assessment (`NO_SNAPSHOT`, "no conclusion drawn"), the wave-B DAG handoff
("271 commits behind … cannot safely narrow the verification set"), the
pinning plan ("1,007 commits behind, advisory and incomplete"). No lane has
ever narrowed a verification set with it.

Its federation with the other tools never materialised: Loomweave's
churn/recent-change views delegate to Warpline and are disabled
(`integrations.warpline.enabled: false`); Filigree's `warpline_worklist_ingest`
has never been applied (0 rows); every enrichment field on every call was
`absent`, `unavailable`, or `stale`. Cost of keeping it: a 374 MB store, a
4.3 s SessionStart hook sitting 0.7 s under its own timeout, and a standing
invitation for agents to trust an answer that is ~80 % incomplete without
saying so.

## Decision

Remove Warpline: the `.mcp.json` server, the SessionStart hook, the
`AGENTS.md` installer block, both `warpline-workflow` skill copies, the
managed block in `.git/hooks/post-commit`, and the `.weft/warpline` store.
A guidance test pins the absence of each surface.

The change-impact question is answered by the tools that measured correctly:
Loomweave for "who calls / references this" and git (`diff`, `log -L`,
`grep` under `tests/`) for "what changed and which tests touch it". A
re-verify worklist that cannot see worktree commits is worse than no
worklist, because it invites narrowing the gate on incomplete evidence — the
full `pytest tests/` run before merge stays the rule.

## Consequences

- Nothing measured is lost; no decision in the project's history rested on a
  Warpline answer.
- Loomweave's `entity_high_churn_list` / `entity_recent_change_list` remain
  empty (they were already disabled). If churn ranking becomes a need, git
  provides it directly.
- Historical assessment artifacts under `docs/architecture/**/assessments/`
  that captured Warpline output are left as written; they document that the
  output was unusable at the time.
