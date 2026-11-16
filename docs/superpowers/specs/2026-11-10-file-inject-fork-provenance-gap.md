# `file_inject` upload-only guard: fork provenance gap

**Status:** closed for `file_inject` by the ruling below, and for the `llm`
prompt-surface guard by option 1 below (dta-au/elspeth#365):
`copy_blobs_for_fork` carries the source's modality and `creating_*` fields and
anchors an LLM-authored copy to the child's `session_fork` plan row. Fork
copies made before that change keep `VERBATIM`. The paragraph that follows
records the state at the ruling, when the `llm` guard was still open.
Maintainer ruling, 2026-11-11: an agent write is an agent write. `update_blob`
now records any blob it rewrites with `created_by == "assistant"`, as it
records a file the agent created new, so a rewritten upload and every fork copy
of it fail `is_user_uploaded_blob`, in the same session and after a fork. None
of the four options below was taken. What stays open is the modality-only
`llm` guard, which a fork copy of a model-written blob still passes
(dta-au/elspeth#365); the prompt review card covers that route. Uploads
rewritten before the change keep their old creator. The text below is the
analysis as recorded at branch head `85deed575` (`claude/happy-cray-mbfmm1`),
before the ruling. Parent design: `2026-11-10-file-inject-design.md` § "Web Composer".

## The route

In the Web Composer, a `file_inject` entry's content must be a file the user
uploaded: `created_by == "user"` and a creation modality that is not
LLM-authored (`is_user_uploaded_blob`,
`src/elspeth/web/execution/_validation_materialization.py`). That rule can be
got around in three steps:

1. The user uploads a file. It is `user` / `VERBATIM`.
2. The planner overwrites it with `update_blob`. The blob keeps `created_by`
   and becomes `LLM_GENERATED`. It is now correctly refused for `file_inject`.
3. The session is forked. `copy_blobs_for_fork`
   (`src/elspeth/web/blobs/service.py`) copies every ready blob with
   `created_by=source_blob.created_by`, `creation_modality=VERBATIM` and null
   provenance. The copy is `user` / `VERBATIM` and is accepted at authoring,
   `/validate` and run admission.

It needs two user actions (accepting the overwrite where proposals are on, and
a fork). Neither looks like endorsing prompt content. The existing `llm`
prompt-surface guard, which is modality-only, has the same hole independently
of this plugin.

A further leg, read in the code and not run: `_check_shareable_blob`
(`src/elspeth/web/shareable_reviews/service.py`) admits a blob that is `user`
and `VERBATIM`, and the share importer writes each imported file with
`created_by="user"`, `VERBATIM`, null provenance and a fresh id. So a fork copy
of planner-rewritten content can be shared and imported, and arrives
indistinguishable from a real upload. A fix confined to the `file_inject`
guard leaves that open.

## What was checked

Each fact above was confirmed by reading the code. The route was not executed
end to end.

### Why a guard-side check has no reliable marker

The `blobs` row has no column or typed field marking a fork copy. Its columns are id, session,
filename, mime, size, hash, storage path, created_at, created_by, `source_description`, status,
modality, the provenance fields and the transient custody fields. The only fork mark on the row
is the free-text `source_description`, which is not a sound basis for a security check.

Two structured facts exist, but neither is on the blob row:

- the copy's id is `fork_blob_id(target_session_id, source_blob_id)` (a uuid5), which can only
  be checked by someone who already knows the source id;
- the child session holds a `session_fork` audit chat message whose content is schema-tagged
  canonical JSON listing `source_blob_id`, `target_blob_id`, `content_hash`, `size_bytes`
  (`web/sessions/fork_custody.py`, `_fork_blob_plan_content` / `_fork_blob_plan_from_content`),
  and `sessions.forked_from_session_id` names the parent (not a foreign key; the parent may
  have been deleted).

Using them would mean session-database reads across two sessions at all three guard points,
and it can only judge the source blob as it is now, not as it was when forked. Neither
is a column or typed field on the row.

### Why the fork writer cannot simply carry provenance (measured)

`ck_blobs_creating_llm_provenance_nullability` requires an LLM-authored row to carry a non-blank
`created_from_message_id` and all five `creating_*` fields.
`fk_blobs_created_from_message_session` binds `(created_from_message_id, session_id)` to
`(chat_messages.id, chat_messages.session_id)`, `ON DELETE RESTRICT`, and the session engine
runs with `PRAGMA foreign_keys=ON`. The parent's message is not in the child session:
`fork_session` gives every copied message a fresh `uuid4`, keeps the source→copy map only in
memory, and copies only messages before the fork point, while the plan copies every ready blob.

Experiment (reverted; tree was clean afterwards): `copy_blobs_for_fork` temporarily changed to
carry the source's modality and provenance, and a temporary test that turned a user upload's
row into the `update_blob` shape and forked it:

```
.venv/bin/python -m pytest tests/unit/web/blobs/test_service.py -n 0 -k zz_experiment
E   sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) FOREIGN KEY constraint failed
1 failed, 220 deselected
```


### A third route the review did not list

`shareable_reviews.service._check_shareable_blob` admits a blob that is `created_by == "user"`
and `VERBATIM`, and `shareable_reviews/importer.py` writes each imported file with
`blob_service.create_blob(..., created_by="user")`, which is a fresh `uuid4`, `VERBATIM`, null
provenance. So a fork copy of planner-rewritten content can also be shared and imported, and
arrives indistinguishable from a real upload. This was read in the code and not run. It
means a guard-only fix inside `file_inject` (including any reading of the fork plan) leaves the
laundering open through share and import; only a fix in the fork writer closes it.

### Options

1. **Fork writer carries modality and `creating_*`, with `created_from_message_id` re-anchored
   to a message in the child session** (for example the fork's own `session_fork` audit plan
   message, which exists before the copy runs). No schema change. Closes `file_inject`, the
   `llm` prompt-surface guard and the share/import route together. Cost: `created_from_message_id`
   stops meaning "the user message that triggered creation" for a copy, and `ON DELETE RESTRICT`
   then pins that child message while the blob exists. Fork tests that assert copies are
   `VERBATIM` with null provenance change. Recommended by the implementer and the controller if the anchor meaning is acceptable.
2. **Schema change**: a nullable `forked_from_blob_id` (or a relaxed anchor rule for copies).
   Cleanest record, needs a migration; none was written.
3. **Guard reads the fork plan** as described above. No writer or schema change, but adds
   cross-session reads at authoring, `/validate` and run admission, judges the source's current
   state only, leaves the `llm` guard and share/import open.
4. **Interim, `file_inject` only**: accept only a user-created, non-LLM blob whose id is a
   `uuid4` (uploads are `uuid4()` in `BlobServiceImpl.create_blob`; fork copies are `uuid5`).
   One-line guard change, every fork copy is refused and a forked session needs the file
   uploaded again. It leans on an id-generation convention rather than a declared field, and it
   does not close share/import.

### Same-session variant (judged, not changed)

`_blob_creation_provenance` returns `VERBATIM` when the new content is a non-empty substring of
the current turn's user message. So `update_blob` on a user upload with such text leaves the
blob `user` / `VERBATIM` and `file_inject` accepts it. **Yes, this lets planner-chosen text
through**: every character was typed by the user in that message, but the planner chooses which
contiguous piece, and a piece can reverse the meaning ("do not ignore the refund policy" →
"ignore the refund policy"). It is never a file the user uploaded.

Recommendation: for `file_inject`, additionally require `created_from_message_id is None`.
The upload routes always write `None` (`BlobServiceImpl.create_blob`), and `update_blob` always
stamps the turn's message id (the verbatim branch requires one; the LLM branch raises without
one). That is a structured column, a one-line change in `is_user_uploaded_blob`, and it closes
this variant completely. It does not help the fork route, because the fork copy resets the
column to `None`. The change was not made; it was in scope for judgement only.

## What exists in the tree

- No fix for this route, and no failing test for it: the experiment under
  "Why the fork writer cannot simply carry provenance" was reverted.
- The parent design's "Web Composer → Upload-only guard" section carries a
  dated "Known gap" paragraph pointing here.
- Tests that currently pin a fork copy of a genuine upload as ACCEPTED
  (`tests/unit/web/composer/test_file_inject_upload_only_tool_parity.py`,
  `tests/unit/web/execution/test_file_inject_upload_only_readiness.py`) will
  need to change under option 4, and gain a refusal case for the overwritten
  upload under any option.
