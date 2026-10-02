# Runbook: Resume Failed Run

Resume a pipeline that crashed or was interrupted.

---

## Symptoms

- Pipeline process terminated unexpectedly
- Run status shows `running` but process is not active
- Error message: "Run already in progress"

---

## Prerequisites

- Access to the audit database
- Access to the configuration file used for the original run
- The `state/` directory from the original run (contains checkpoints)
- The original payload store, including any sealed source snapshot and row
  payloads. Retain it together with the audit database and checkpoints.

---

## Opt-in Source Snapshots

For a finite, single-source CSV or JSON pipeline, set
`snapshot_for_resume: true` in the source options **before the original run**.
For example, this source section routes accepted rows to an existing sink named
`output`:

```yaml
sources:
  primary:
    plugin: csv
    on_success: output
    options:
      path: input.csv
      snapshot_for_resume: true
      schema:
        mode: observed
      on_validation_failure: discard
```

The flag defaults to `false`. It is supported by the `csv` and `json` source
plugins, including the JSON plugin's JSONL format, for normal live execution.
A pipeline with multiple sources refuses this option.

Snapshot mode parses and validates the entire finite source and seals its
emitted rows in the payload store before any row reaches a downstream
transform or sink. The serialized snapshot is limited to **64 MiB**; this is
a spool-size limit, not a RAM limit or an input-file-size limit. Parsing and
holding the snapshot can use more memory, and downstream processing starts
only after sealing completes. An oversized snapshot fails before downstream
row processing.

Once the snapshot and completed source lifecycle are recorded, resume can
process the remaining sealed emissions even if the original file has changed
or been deleted. It uses the original row contracts, source row indexes,
quarantine decisions and validation-error identities; it does not reopen the
file or rerun source validation. Source rows discarded during validation stay
discarded.

Keep the original source options and the rest of the compatible configuration
for the resume attempt. Check eligibility first, then execute:

```bash
elspeth resume <RUN_ID> --settings pipeline.yaml --database ./runs/audit.db
elspeth resume <RUN_ID> --settings pipeline.yaml --database ./runs/audit.db --execute
```

A crash before sealing and source-lifecycle completion still leaves the source
incomplete and resume refuses. Missing or corrupt snapshot references,
metadata or payload bytes also refuse recovery; the current input file is not
a fallback. The usual checkpoint, authority and sink-effect checks still
apply. In particular, a pipeline containing `web_scrape` with a non-GET method
(including POST search) refuses automatic resume because the prior remote
request may have succeeded without its audit call being recorded. A sealed
source does not resolve that uncertainty.

---

## Procedure

### Step 1: Identify the Failed Run

Find the run ID of the failed run:

```bash
# List recent runs and their status
sqlite3 runs/audit.db "
  SELECT run_id, status, started_at, completed_at,
         (SELECT COUNT(*) FROM rows WHERE rows.run_id = runs.run_id) as rows_processed
  FROM runs
  ORDER BY started_at DESC
  LIMIT 10;
"
```

Look for runs with status `running` that have no `completed_at` timestamp.

### Step 2: Check Checkpoint State

Verify checkpoints exist for the run:

```bash
sqlite3 runs/audit.db "
  SELECT checkpoint_id, row_id, token_id, created_at
  FROM checkpoints
  WHERE run_id = '<RUN_ID>'
  ORDER BY created_at DESC
  LIMIT 5;
"
```

If no checkpoints exist, the run cannot be resumed - you must start fresh.

### Step 3: Check Resume Compatibility

```bash
elspeth resume <RUN_ID>
```

Without `--execute`, the resume command checks whether the run can be resumed,
reports the resume point, and validates checkpoint compatibility. To actually
continue processing:

```bash
elspeth resume <RUN_ID> --execute
```

The resume command:
1. Loads the settings needed to validate checkpoint compatibility
2. Finds the last valid checkpoint
3. Continues processing from that point when `--execute` is present
4. Records all events with the same run ID

### Step 4: Verify Completion

After the run completes:

```bash
# Check run status
sqlite3 runs/audit.db "SELECT status, completed_at FROM runs WHERE run_id = '<RUN_ID>';"

# Verify row and terminal-token counts
sqlite3 runs/audit.db "
  SELECT
    (SELECT COUNT(*) FROM rows WHERE run_id = '<RUN_ID>') as source_rows,
    (SELECT COUNT(*) FROM token_outcomes WHERE run_id = '<RUN_ID>' AND completed = 1) as terminal_tokens;
"
```

In linear pipelines the terminal-token count usually matches source rows. In
fork, expand, batch, or coalesce pipelines, use `elspeth explain` for a sample
row and confirm every live token reached an expected terminal path.

---

## Docker Resume

When running in Docker, select an exact tag that you have confirmed exists in
the registry. Use the same immutable image identity that created the
checkpoint unless a reviewed compatibility decision approves a newer image:

```bash
: "${IMAGE_TAG:?export an exact published sha-* or v* image tag}"
docker buildx imagetools inspect \
  "ghcr.io/dta-au/elspeth:${IMAGE_TAG}" >/dev/null

docker run --rm \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/input:/app/input:ro \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/state:/app/state \
  ghcr.io/dta-au/elspeth:${IMAGE_TAG} \
  resume <RUN_ID> --execute
```

**Important:** Mount the same `state/` directory that contains the original run's checkpoints.

---

## Troubleshooting

### Pre-2026-01-24 Checkpoints Are Invalid

All checkpoints created before 2026-01-24 are invalid due to node ID format changes introduced in the routing refactor. Attempting to resume from a pre-2026-01-24 checkpoint will fail. Delete old checkpoint files and re-run affected pipelines.

The useful migration rule from the old RC-2 checkpoint note is:

1. Checkpoints from the old `token_ids`-only format cannot be restored.
2. Delete old checkpoint files for the affected run.
3. Re-run the pipeline so new checkpoints store full token metadata.

Full historical context is preserved in the RC-2 checkpoint fix post-mortem in
git history or maintainer-local archives.

### "Run not found"

The run ID doesn't exist in the audit database:

```bash
# List all run IDs
sqlite3 runs/audit.db "SELECT run_id FROM runs;"
```

### "No checkpoint available"

The run crashed before creating any checkpoints. Start a new run instead:

```bash
elspeth run --settings pipeline.yaml --execute
```

### "Configuration mismatch"

The resume command validates the checkpoint against the settings it loads for
the resume attempt. If you need different settings, start a new run instead of
resuming the old one.

### "Duplicate row_id"

The checkpoint was corrupted or source data changed. Options:
1. Start a fresh run.
2. Preserve the failed run's audit database and checkpoint files if they are
   needed for incident evidence.

### A row raised an unexpected error

The run stopped with a traceback from a transform (a plugin bug or an ELSPETH
failure, on the leader or on an `elspeth join` follower), not with a routed
row error. The row's work item is left `failed` with no outcome, and the run
cannot be recorded as completed while it is: a finalization over it is
refused with `FAILED scheduler work whose token has no terminal outcome`.

In ordinary streaming mode (`snapshot_for_resume: false`), resume can process
the row again only if the run had finished reading its source before it
stopped. For example, the error came after an aggregation or
collector that holds rows until the end of the source, or on a follower while
the leader finished reading. If the source was still being read, as in a
single-process run where a transform raised mid-stream, resume refuses with
`source lifecycle is incomplete (…) — resume replays only persisted row
payloads, so unread source rows may exist; start a fresh run`. That refusal
comes before any row is requeued. Fix the cause and start a fresh run.

With [snapshot mode](#opt-in-source-snapshots), the finite source is already
sealed and recorded complete before downstream rows start. A downstream
failure can therefore leave both failed scheduler work and unprocessed sealed
emissions for resume. Recovery still requires the retained snapshot, compatible
checkpoint and the other resume gates described above.

Otherwise:

1. Fix the cause.
2. Run `elspeth resume <RUN_ID> --execute`. Resume returns each such row to
   the queue, records a `resume_requeue_failed` scheduler event for it, and
   processes it again from the node where its work started, under a new
   attempt number. A row that already has an outcome, including one routed to
   `on_error`, is not processed again.

Notes:

- The row is processed at least once more: an external call the failed
  attempt made before it raised may be made again, as when a crashed worker's
  row is taken over.
- If the cause is not fixed, resume fails the same way, the run stays
  `failed` and nothing is recorded as completed. Resume again after the fix.
  If that failed resume had already taken a row waiting for its sink, the row
  is held until its item lease (300 seconds) lapses; a resume before then
  processes the requeued row but stops with `residual scheduler work`, and a
  later resume finishes the run.
- `elspeth abandon` refuses a `failed` run as resumable. To give up on the
  run instead, start a fresh one.

### "source lifecycle is incomplete" on a run that is still `running`

The run's leader died (crash, SIGKILL, evicted replica) before its source was
recorded `exhausted`, and the run is stuck `running` with an expired seat.
Resume refuses because it cannot prove that all source emissions were
retained. This covers ordinary streaming runs interrupted before source
completion and snapshot runs interrupted before sealing/lifecycle completion.
No resume can recover such an incomplete source; finalize the run
honestly instead:

```bash
# Dry run: shows the dead seat, source states, and the undecided work
elspeth abandon <RUN_ID> --settings pipeline.yaml --database ./runs/audit.db

# Take the dead seat and finalize the run as interrupted
elspeth abandon <RUN_ID> --settings pipeline.yaml --database ./runs/audit.db --execute
```

Undecided tokens are recorded as `abandoned` (ADR-038), followers are
departed, and the seat is vacated. Reprocess the source with a fresh run. If
the dry run reports `Resumable: yes`, use `elspeth resume` instead.

---

## Prevention

To reduce resume scenarios:

1. **Use frequent checkpoints** for critical pipelines:
   ```yaml
   checkpoint:
     enabled: true
     frequency: every_row
   ```

2. **Monitor pipeline processes** with health checks

3. **Use Docker with restart policies**:
   ```yaml
   services:
     elspeth:
       restart: on-failure:3
   ```

---

## See Also

- [Incident Response](incident-response.md) - For investigating root cause
- [Scheduler Lease Recovery](scheduler-lease-recovery.md) - For diagnosing stuck `leased` work items, SCREAM invariants, and lease-expiry churn before invoking `elspeth resume`
- [Configuration Reference](../reference/configuration.md#checkpoint-settings) - Checkpoint configuration
