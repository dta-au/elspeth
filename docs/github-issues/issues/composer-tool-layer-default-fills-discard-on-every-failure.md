---
title: Composer tool layer default-fills "discard" on every failure edge
labels: [area/composer, type/bug]
---

The composer's tool layer silently supplies `"discard"` for every unset failure-routing field before the value reaches `NodeSpec`, inventing a routing decision the author never made and suppressing a validation error that exists to demand one.

## What happens

A pipeline authored through the composer arrives with `on_error`, `on_write_failure` and `on_validation_failure` already set to `"discard"` — failed rows are dropped — whether or not the author ever chose that. Because the fill happens above the specification layer, the author is never asked, and the disclosure record cannot distinguish a chosen `"discard"` from a supplied one.

The engine treats all three as required author decisions with no default: `on_error` at `src/elspeth/core/config.py:1480`, `on_write_failure` at `src/elspeth/core/config.py:1535-1542` (marked "Required — no default"), and source `on_validation_failure` at `src/elspeth/plugins/infrastructure/config_base.py:494` ("All sources must specify where non-conformant rows go"). Only the composer layer supplies one.

## Why

`src/elspeth/web/composer/state.py:764-779` carries an explicit reasoned prohibition against defaulting `on_error`: a transform's `on_error` has no runtime default, so defaulting it "would INVENT a routing decision the author never made, and 'discard' silently drops failed rows in a system whose purpose is lineage".

That prohibition is defeated by construction sites above `NodeSpec`:

- `src/elspeth/web/composer/tools/transforms.py:793` (`upsert_node`) — `on_error=validated.on_error or ("discard" if node_type in ("transform", "aggregation") else None)`
- `src/elspeth/web/composer/tools/transforms.py:1989` (`splice_transform`) — `on_error or "discard"`
- `src/elspeth/web/composer/tools/sessions.py:1730` (`set_pipeline`) — the same expression
- `src/elspeth/web/composer/tools/outputs.py:49` — `on_write_failure: str = "discard"`
- `src/elspeth/web/composer/tools/_common.py:1773` — `_DEFAULT_SOURCE_VALIDATION_FAILURE = "discard"`

A consequence is that `transform_missing_on_error` (`src/elspeth/web/composer/state.py:7760`) is unreachable from any composer tool surface: the fill runs first, so the error can never fire for a composer-authored transform.

This re-creates the implicit routing that ADR-004 (`docs/architecture/adr/004-adr-explicit-sink-routing.md`) abolished, on the failure axis. It is a defect against an existing decision, not a new design question.

## Impact

Every pipeline authored through the composer. The blast radius is the difference between "the author decided failed rows are droppable" and "nobody decided" — a distinction the audit trail is meant to preserve. `_routing_provenance` (`src/elspeth/web/composer/implicit_decisions.py:491-500`) now labels a `"discard"` value `"default"` rather than `"composer_selected"`, which makes the disclosure honest but does not restore the author's choice.

## Fix

The three edges are not equally removable.

- **Transform and aggregation `on_error`** — removable. A Stage-1 error code and repair guidance already exist; deleting the fill restores `state.py:764-779` to force.
- **Sink `on_write_failure`** — removable. The default is a pure tool-layer invention with no runtime counterpart.
- **Source `on_validation_failure`** — the value cannot simply be flipped. Quarantine routing requires naming an existing sink (enforced in `src/elspeth/engine/orchestrator/validation.py`), and no sink exists at source-authoring time. The available lever is forced explicitness plus honest disclosure, not a different default.

Removal is gated by the fixed-script tutorial canary, ADR-031 (`docs/architecture/adr/031-tutorial-is-a-fixed-script-canary.md`): if the tutorial transcript omits `on_error`, removing the fill breaks it on the first commit. The fill is also pinned by `tests/unit/web/composer/test_set_pipeline_candidate.py`, `tests/unit/web/composer/guided/test_state_from_proposal.py` and `tests/unit/web/composer/test_yaml_generator.py`, each asserting the supplied `"discard"`.

One conflict must be settled first. On nodes covered by a required control, `"discard"` may currently be the only legal `on_error` value, because routing to a quarantine sink is an uncontrolled write path that coverage rejects. Until it is decided whether "covered" means screened before write or before release, the fix cannot reach those nodes.

Done looks like: no composer tool supplies a failure route; an unset `on_error` produces `transform_missing_on_error`; and the disclosure record shows a route the author named, or no route at all.
