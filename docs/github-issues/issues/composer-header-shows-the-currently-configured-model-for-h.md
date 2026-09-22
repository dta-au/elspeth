---
title: Composer header chip shows the currently configured model, not the model that authored the session
labels: [area/composer, area/web, area/audit, type/bug]
---

The session header's `Composer: <name>` chip renders the deployment's current
`ELSPETH_WEB__COMPOSER_MODEL` setting rather than the model that actually composed the
session being viewed. Anyone reviewing a historical session is shown the wrong provenance.

## What happens

Measured on a staging deployment on 2026-09-13. One session composed on 2026-09-07 records
its authoring model in its interpretation events — three fields, all stamped at composition
time:

    model_identifier: <provider>/<vendor>/<model-A>
    model_version:    <vendor>/<model-A>
    provider:         <provider>

After `ELSPETH_WEB__COMPOSER_MODEL` was changed to a different model and the service
restarted, that same unmodified session's header rendered the new model's display name. The
session had not been recomposed — its composition state was unchanged from 2026-09-07.

## Why

The chip is deployment configuration by construction.
`src/elspeth/web/frontend/src/components/chat/ModelChip.tsx` reads `composerModel` from the
browser session store, which the application's health poll publishes from the system status
endpoint; the component's own header comment states that the value is
`ELSPETH_WEB__COMPOSER_MODEL`. It is never reconciled against the session being displayed.
The same component renders an advisor-model chip from the same configuration source.

The per-session truth is already served: `GET /api/sessions/{id}/interpretations`
(`src/elspeth/web/sessions/routes/interpretation.py:226`) returns per-event
`model_identifier`, `model_version` and `provider`, stamped at composition time.

## Impact

ELSPETH's compose record is meant to explain and reproduce what happened. A header that
asserts the wrong authoring model contradicts the deployment's own stored evidence, and does
so with an authoritative appearance — worse than showing nothing.

## Where the work starts

`src/elspeth/web/frontend/src/components/chat/ModelChip.tsx` and the session store it reads
(`src/elspeth/web/frontend/src/stores/sessionStore.ts`). The data the chip should be using
comes from the interpretations route named above. `ModelChip.test.tsx` sits beside the
component.

## Fix — what needs deciding first

A session can legitimately span models: composed under one, continued after a configuration
change under another. So "the model" may not be single-valued, and the product decision comes
before the code. Showing the model of the latest composition state, showing a range, or
relabelling the chip explicitly as the current *setting* are all defensible. Whichever is
chosen, the chip must not assert provenance it cannot support.

The component deliberately does not fetch — it reads what the single health poll published, to
avoid a second consumer of that endpoint — so whatever source is chosen must be published into
the store the same way rather than fetched from the chip.

## Done looks like

A component test that renders a session whose interpretation events name model A while the
configured model is B, and asserts the header does not claim B authored that session.

## Scope note

Only the header chip was checked. Whether the pipeline tab, audit story, shareable review
export or YAML export carry the same current-setting-as-provenance substitution has not been
measured, and is worth sweeping as part of the fix.

## Size

Small once the display decision is made — one component, one store field, one test. The
unmeasured surfaces in the scope note could make it larger; measure them before estimating.

## Repro

1. Compose a session with composer model A.
2. Change `ELSPETH_WEB__COMPOSER_MODEL` to B in the deployment environment file and restart
   the web service.
3. Reopen the session from step 1. The header shows B; its interpretation events still
   record A.
