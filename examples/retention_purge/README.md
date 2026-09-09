# Retention/Purge Example

Demonstrates the payload retention lifecycle: store large blobs during processing, then purge them after the retention period while preserving the audit trail.

## What This Shows

A document classification pipeline with an explicit `payload_store` configuration. After a run completes, `elspeth purge` removes old payloads while the Landscape audit trail (hashes, metadata, lineage) remains intact.

```
source ─(validated)─> passthrough ─(processed)─> [classification_gate] ─┬─ public.csv
                                                                         └─ restricted.csv
```

## Running

### Step 1: Run the pipeline

```bash
elspeth run --settings examples/retention_purge/settings.yaml --execute
```

### Step 2: Inspect the payloads

```bash
# Payloads are stored in the configured directory
ls examples/retention_purge/runs/payloads/
```

### Step 3: Purge old payloads

```bash
# Dry run — see what would be deleted
elspeth purge --dry-run \
  --database examples/retention_purge/runs/audit.db \
  --payload-dir examples/retention_purge/runs/payloads \
  --retention-days 7

# Execute purge after the configured retention period has elapsed
elspeth purge --yes \
  --database examples/retention_purge/runs/audit.db \
  --payload-dir examples/retention_purge/runs/payloads \
  --retention-days 7
```

The CLI requires a positive retention period. A freshly completed run is
therefore ineligible for deletion; both commands report no expired payloads
until the run is more than seven days old. The explicit flag matters when
running from the repository root: `purge` only auto-loads a `settings.yaml` in
the current directory and otherwise defaults to 90 days.

### Step 4: Verify audit trail survives

```bash
# interactive TUI (a row/token selector is optional in interactive mode)
elspeth explain \
  --run latest \
  --settings examples/retention_purge/settings.yaml \
  --database examples/retention_purge/runs/audit.db

# Non-interactive text output requires a row or token selector
DB=examples/retention_purge/runs/audit.db
ROW_ID="$(sqlite3 "$DB" \
  'SELECT row_id FROM rows ORDER BY row_index LIMIT 1')"
elspeth explain \
  --run latest \
  --row "$ROW_ID" \
  --no-tui \
  --settings examples/retention_purge/settings.yaml \
  --database "$DB"
```

Supplying the settings file gives `explain` the configured payload-store
location, so it can resolve retained payload content instead of reporting it
as unavailable.

## Payload Store Configuration

```yaml
payload_store:
  backend: filesystem                           # Storage backend
  base_path: examples/retention_purge/runs/payloads  # Where blobs are stored
  retention_days: 7                             # Days before eligible for purge
```

## What Gets Purged vs Preserved

| Purged (blob content) | Preserved (audit trail) |
|-----------------------|------------------------|
| Row source data | Row metadata + hashes |
| Operation input/output bodies | Operation records + content hashes |
| External call request/response | Call records + timing + hashes |
| Routing reason payloads | Routing events + decision metadata |

**The key insight**: Hashes survive payload deletion. An auditor can still verify "this row was processed with this input and produced this output" — they just can't see the raw content after purge. This is by design for compliance scenarios where data retention policies require deletion but audit trail must persist.

## Purge CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--database` / `-d` | Auto-detect | Path to Landscape database |
| `--payload-dir` / `-p` | Auto-detect | Path to payload storage |
| `--retention-days` / `-r` | From config | Age threshold for deletion |
| `--dry-run` | Off | Show what would be deleted |
| `--yes` / `-y` | Off | Skip confirmation prompt |

## Key Concepts

- **Content-addressable storage**: Payloads stored by content hash (deduplication)
- **Retention policy**: Only payloads from completed/failed runs older than `retention_days` are eligible
- **Running pipelines are protected**: Active run payloads are never deleted
- **Reproducibility impact**: Purging downgrades run from `REPLAY_REPRODUCIBLE` to `ATTRIBUTABLE_ONLY`
