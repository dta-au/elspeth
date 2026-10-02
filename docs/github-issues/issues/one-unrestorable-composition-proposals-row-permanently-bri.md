---
title: One unrestorable composition proposal row bricks a session, and no API can retire it
labels: [area/composer, area/web, type/bug]
---

A single `composition_proposals` row that fails to restore makes every subsequent composer
turn in that session raise, and every API path that could retire the row goes back through
the same restore. The session becomes unusable with no supported recovery.

## Background

A composition proposal is a stored record of a pipeline the composer proposed during a
session. Rows live in the `composition_proposals` table; restoring one means rebuilding a
`PipelineProposal` object from the stored JSON and re-verifying its integrity hashes.

## Why

Every composer turn preflights tool-call IDs. `_preflight_session_tool_call_ids`
(`src/elspeth/web/composer/tool_batch.py:293`) calls
`sessions_service.list_composition_proposals(session_id)` with no `status` argument
(`tool_batch.py:314`).

`list_composition_proposals` (`src/elspeth/web/sessions/service.py:8320`) declares
`status: ProposalLifecycleStatus | None = None` and skips its `WHERE status = ...` clause
when the argument is `None` (`service.py:8332-8333`), so it selects every row for the
session — `pending`, `committed` and `rejected` alike. For each row it calls
`_classify_authoritative_composition_proposal` (`service.py:3078`), which for a
`set_pipeline` row calls `_restore_authoritative_pipeline_proposal` (`service.py:2317`,
reached at `service.py:3104`). That constructs
`PipelineProposal(pipeline=deep_thaw(row.arguments_json), ...)` (`service.py:2406`), whose
`__post_init__` re-verifies the draft hash and, for owned-state authorities, the round trip.

So one unrestorable row raises on every later turn of that session, not only when that
proposal is listed or acted on — chat, proposals and anything else that preflights tool-call
IDs all stop.

## No way out

Every retirement path restores first: `reject_pipeline_composition_proposal`
(`service.py:8186`), `reject_guided_pipeline_proposal` (`service.py:13145`),
`revert_state_for_guided_operation` (`service.py:10839`) and
`back_edit_guided_pipeline_proposal` (`service.py:12848`) each call
`_restore_authoritative_pipeline_proposal`. The only remaining remedy is hand-written SQL
against the session database plus a synthetic `proposal_events` row, which is exactly the
ceremony a fail-closed control should make unnecessary.

## Impact

Latent, and total when it fires. The restore path has 30 `raise` sites in the version
checked (`service.py:2317-2509`) — a draft-hash mismatch, a provenance binding mismatch, a
malformed base or a corrupt creation event each produce the same permanent loss. Better
diagnostics for any one cause would make the reason legible without giving the operator an
action. In the development database checked at the time, all 26 proposals restored cleanly,
so no live instance is known.

## Where the work starts

Two files: `src/elspeth/web/composer/tool_batch.py` for the preflight that triggers it, and
`src/elspeth/web/sessions/service.py` for the restore and the retirement APIs. The service
module is very large — navigate it by the symbol names above rather than by scrolling.

## Done looks like

A test that persists a proposal row which cannot be restored, and then asserts both of:

1. an ordinary composer turn in that session still completes; and
2. the row can be retired through a supported API call, with the resulting
   `proposal.rejected` event written and no `PipelineProposal` constructed.

Neither holds today.

## Fix — what needs deciding

The full shape is an abandon or quarantine path that retires a row **without** restoring it:
authorise on `(session_id, proposal_id)` and the row's own status, write the
`proposal.rejected` event from the row's persisted columns, and never construct a
`PipelineProposal`. The restore is what is broken, so retirement must not depend on it. What
authorises that call, and what the event records when the row's contents cannot be trusted,
are open questions for the maintainer.

A narrower change stands on its own and does not need that decision: give
`_preflight_session_tool_call_ids` a projection that reads only `tool_call_id` and does not
classify authority, so an unrestorable row stops breaking unrelated turns. That stops the
bricking but does not give the operator a way to retire the row.

## Size

The narrow projection change is small and self-contained — a good first issue. The abandon
path is a design decision first and should not be started until the authorisation question
above has been decided. Neither has been built.
