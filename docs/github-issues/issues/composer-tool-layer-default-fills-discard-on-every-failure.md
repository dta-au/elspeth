---
title: Composer tool layer default-fills "discard" on every failure edge
labels: [area/composer, type/bug]
---

The Composer's tool layer silently supplies `"discard"` for every unset failure-routing field, inventing a routing decision the author never made and suppressing the validation error that exists to demand one.

## Where this lives

The Composer is the authoring surface that builds a pipeline graph. It has two layers that matter here:

- **The tool layer**, `src/elspeth/web/composer/tools/` — the functions the planner (the model that authors the pipeline in conversation with the user) calls to add and edit nodes (`upsert_node`, `splice_transform`, `set_pipeline`, `set_output`). This is where the defect is.
- **The specification layer**, `src/elspeth/web/composer/state.py` — the frozen `NodeSpec`/`OutputSpec` dataclasses the tools construct, plus the `validate()` pass over them.

Three fields carry failure routing. Each names where a row goes when something goes wrong, and each accepts either a sink name or the literal `"discard"`, which drops the row:

- `on_error` — a transform or aggregation failed on a row
- `on_write_failure` — a sink could not write a row
- `on_validation_failure` — a source produced a row that did not conform

## What happens

A pipeline authored through the Composer arrives with all three fields already set to `"discard"`, whether or not the author chose that. Failed rows are dropped. Because the fill happens in the tool layer, above the specification layer, the author is never asked, and nothing downstream can tell a chosen `"discard"` from a supplied one.

The engine treats all three as required author decisions with no default:

- `on_error` — `src/elspeth/core/config.py:1480`, a required field
- `on_write_failure` — `src/elspeth/core/config.py:1535-1542`, marked "Required — no default"
- source `on_validation_failure` — `src/elspeth/plugins/infrastructure/config_base.py:494`, "All sources must specify where non-conformant rows go"

Only the Composer layer supplies one.

## Why

`src/elspeth/web/composer/state.py:764-779` carries an explicit prohibition against defaulting `on_error`, with its reasoning in the comment — a transform's `on_error` has no runtime default, so defaulting it would:

```
INVENT a routing decision the author never made, and "discard" silently
drops failed rows in a system whose purpose is lineage
```

Five construction sites above `NodeSpec` defeat it:

- `src/elspeth/web/composer/tools/transforms.py:793` (`upsert_node`) — `on_error=validated.on_error or ("discard" if node_type in ("transform", "aggregation") else None)`
- `src/elspeth/web/composer/tools/transforms.py:1989` (`splice_transform`) — `on_error or "discard"`
- `src/elspeth/web/composer/tools/sessions.py:1730` (`set_pipeline`) — the same expression
- `src/elspeth/web/composer/tools/outputs.py:49` — `on_write_failure: str = "discard"`
- `src/elspeth/web/composer/tools/_common.py:1773` — `_DEFAULT_SOURCE_VALIDATION_FAILURE = "discard"`

One consequence is that the validation error `transform_missing_on_error` (`src/elspeth/web/composer/state.py:7760`) is unreachable from any Composer tool surface. The fill runs first, so a composer-authored transform can never trigger it.

This re-creates the implicit routing abolished by ADR-004 (`docs/architecture/adr/004-adr-explicit-sink-routing.md`), on the failure axis. It is a defect against an existing decision, not a new design question.

## Impact

Every pipeline authored through the Composer. The blast radius is the difference between "the author decided failed rows are droppable" and "nobody decided" — a distinction the audit trail exists to preserve.

Partial mitigation is already in place: `_routing_provenance` (`src/elspeth/web/composer/implicit_decisions.py:491-500`) labels a `"discard"` value `"default"` rather than `"composer_selected"`, so the disclosure record no longer claims the planner chose it. That makes the record honest; it does not restore the author's choice.

## Fix

**Size: not a small ticket, and one part is blocked.** The code deletion is about five lines. The work is everything around it, and one of the three fields cannot be fixed the same way as the other two.

Correct behaviour, per field:

- **Transform and aggregation `on_error`** — removable. An error code and repair guidance already exist, so deleting the fill restores the prohibition at `state.py:764-779` to force.
- **Sink `on_write_failure`** — removable. The default is a pure tool-layer invention with no runtime counterpart.
- **Source `on_validation_failure`** — the value cannot simply be flipped. Routing a non-conformant row requires naming an existing sink, enforced in `src/elspeth/engine/orchestrator/validation.py`, and no sink exists yet at the point a source is authored. The available lever is forcing the author to be explicit plus recording the choice honestly — not a different default.

Two things will break when the fill is removed, and both are expected rather than regressions:

- The tutorial is a fixed script replayed as a canary, ADR-031 (`docs/architecture/adr/031-tutorial-is-a-fixed-script-canary.md`). If its transcript omits `on_error`, removal breaks it on the first commit and the transcript needs updating.
- The fill is pinned by `tests/unit/web/composer/test_set_pipeline_candidate.py`, `tests/unit/web/composer/guided/test_state_from_proposal.py` and `tests/unit/web/composer/test_yaml_generator.py`, each asserting the supplied `"discard"`. Those assertions are the old behaviour and must be inverted, not deleted.

**Decide this before starting.** On nodes covered by a required control (an admission gate that screens what a node writes), `"discard"` may currently be the only legal `on_error` value, because routing to a quarantine sink is an uncontrolled write path that coverage rejects. Whether "covered" means screened before write or before release is an open question, and until it is answered the fix cannot reach those nodes. Scoping the first change to exclude them is reasonable; doing so silently is not.

Done looks like: no Composer tool supplies a failure route; an unset `on_error` produces `transform_missing_on_error` from the tool surface; and the disclosure record shows either a route the author named or no route at all. You would know by authoring a transform through `upsert_node` without `on_error` and asserting the error comes back, which is exactly the assertion the current tests invert.
