---
title: User customisation and standing-preferences contract
labels: [area/web, area/composer, type/epic]
---

Consolidate per-person presentation settings into one contract: what a person can set, where it is persisted, how it reaches a Composer session, and what the audit trail records about which preferences were in force.

## Scope

Consolidate the per-person settings into a single standing-preferences contract:

- detail level
- default authoring mode
- preferred name
- language variant
- explanation style

Carry the effective preferences into Composer sessions, and audit what was in force for a given session so a later reader can tell which presentation the author saw.

## Constraint

Preferences are presentational and confer no authority. They must not influence graph structure, custody, validation, or required controls. A session authored under any combination of preferences must produce the same pipeline and pass the same gates as one authored under any other. This is the property the epic has to be able to demonstrate, not merely assert.

## Coordination

The persisted fields and their cutover need to be coordinated with the other user-record work in the same release, so the preference fields land with the rest of the per-person schema rather than as a second migration against the same tables.

## Done looks like

A person's preferences are set in one place, persisted in one place, resolved into a Composer session by one path, and visible in the audit record for that session — with a check demonstrating that changing any preference leaves the authored graph, its validation outcome and its required-control coverage unchanged.
