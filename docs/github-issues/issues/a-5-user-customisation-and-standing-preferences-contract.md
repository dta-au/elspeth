---
title: User customisation and standing-preferences contract
labels: [area/web, area/composer, type/epic]
---

Consolidate per-person presentation settings into a single contract: what a person can set, where it is persisted, how it reaches a Composer session, and what the audit trail records about which preferences were in force.

## Where this lives

The Composer is the authenticated web authoring surface where a person builds a pipeline in conversation with a planner model. A **session** is one such authoring conversation, persisted with an audit trail of what was decided and why.

This epic touches the per-person record in the web service (`src/elspeth/web/`) and the point where a Composer session is created and its context assembled. The exact files depend on decisions not yet made, which is why this is an epic rather than a bug — see **Size** below.

## Scope

Bring these per-person settings under one contract:

- detail level — how much explanation the person wants
- default authoring mode
- preferred name
- language variant
- explanation style

Carry the effective preferences into Composer sessions, and record what was in force for a given session so a later reader can tell which presentation the author was working against when they made a decision.

## Constraint

Preferences are presentational and confer no authority. They must not influence graph structure, validation, or required controls — the admission gates that decide whether a node is allowed to write. A session authored under any combination of preferences must produce the same pipeline and pass the same gates as one authored under any other.

This is the property the epic has to demonstrate, not merely assert. It is also the main risk: a preference that quietly changes what the planner proposes would be an authority leak, not a cosmetic bug.

## Coordination

The persisted fields and their cutover need coordinating with the other per-person record work in the same release, so preference fields land with the rest of the user schema rather than as a second migration against the same tables.

## Size

**An epic, and design comes before code.** Nothing here is startable as a single change. At minimum these need deciding first: where preferences are persisted and how they version; how they are resolved into a session (at creation, or read live); what the audit record stores — the values, or a reference to a preference set; and what happens to an existing session when a preference changes mid-flight.

## Done looks like

A person's preferences are set in one place, persisted in one place, resolved into a Composer session by one path, and visible in the audit record for that session.

The authority constraint is demonstrated, not assumed: a check that authors the same pipeline under two materially different preference sets and asserts the resulting graph, its validation outcome and its required-control coverage are identical.
