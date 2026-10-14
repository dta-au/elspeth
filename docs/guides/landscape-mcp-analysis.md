# Landscape MCP Analysis Server

Current as of 2026-10-13 (0.8.4 SQL contract).

`elspeth-mcp` is a read-only MCP server for querying an ELSPETH Landscape audit
database. It is intended for debugging, audit investigation, and agent-assisted
analysis. It is not a production write surface.

The authoritative tool registry is `_TOOLS` in
`src/elspeth/mcp/server.py`; analyzer behavior is implemented through
`src/elspeth/mcp/analyzer.py` and `src/elspeth/mcp/analyzers/`.

## Quick Start

Install with MCP support:

```bash
uv sync --frozen --extra mcp
source .venv/bin/activate
```

Run with database auto-discovery:

```bash
elspeth-mcp
```

Run against an explicit Landscape database:

```bash
elspeth-mcp --database sqlite:///./examples/threshold_gate/runs/audit.db
```

Or use an environment variable:

```bash
export ELSPETH_DATABASE_URL=sqlite:///./state/audit.db
elspeth-mcp
```

## Database Discovery

When `--database` is not provided, the server searches downward from the current
directory, prefers `runs/audit.db`, accepts `landscape.db`, sorts candidates by
modification time, and selects the best non-interactively for MCP clients. In an
interactive terminal it can prompt for the target database.

## Claude Code Configuration

```json
{
  "mcpServers": {
    "elspeth-landscape": {
      "command": "elspeth-mcp",
      "args": [],
      "description": "ELSPETH Landscape audit database analysis"
    }
  }
}
```

With an explicit database:

```json
{
  "mcpServers": {
    "elspeth-landscape": {
      "command": "elspeth-mcp",
      "args": ["--database", "sqlite:///./examples/my_pipeline/runs/audit.db"]
    }
  }
}
```

## When To Use

- Diagnose failed, stuck, or high-error runs.
- Explain a row or token through the pipeline DAG.
- Inspect source/sink operations and their external calls.
- Investigate routing, batch, coalesce, and terminal-outcome behavior.
- Inspect schema contracts and contract violations for a run.
- Run tightly scoped read-only SQL when the maintained analysis tools are not
  enough.

For operator command-line investigations, start with
[`elspeth explain`](../runbooks/investigate-routing.md). Use MCP when an MCP
client needs structured, iterative audit queries.

## Tool Registry

The current registry exposes 33 tools:

| Category | Tools |
|----------|-------|
| Run queries | `list_runs`, `get_run`, `get_run_summary` |
| Graph and rows | `list_nodes`, `list_rows`, `list_tokens`, `list_group_records`, `list_group_losses`, `get_token_children`, `get_dag_structure` |
| Lineage | `get_token_lineage`, `explain_token`, `get_node_states`, `get_calls`, `get_errors`, `list_collisions` |
| Operations | `list_operations`, `list_artifacts`, `get_sink_effect_history`, `get_operation_calls`, `list_verification_decisions` |
| Reports | `get_performance_report`, `get_error_analysis`, `get_llm_usage_report`, `get_outcome_analysis`, `describe_schema` |
| Diagnostics | `diagnose`, `get_failure_context`, `get_recent_activity` |
| Contracts | `get_run_contract`, `explain_field`, `list_contract_violations` |
| SQL | `query` |

Refresh the list from source with:

```bash
python - <<'PY'
from elspeth.mcp.server import _TOOLS
for name in _TOOLS:
    print(name)
PY
```

## Common Workflows

### Something Failed

1. Run `diagnose`.
2. Use `get_recent_activity` to identify the affected run.
3. Use `get_failure_context` for failed states and error patterns.
4. Use `explain_token` on a representative token or row.

### A Row Was Routed Unexpectedly

1. Use `list_rows` to find the row.
2. Use `list_tokens` to find terminal and branch tokens for that row.
3. Use `explain_token` with `token_id` when the row forked or expanded.
4. Use `list_collisions` if merged context fields or union origins are in
   question.

### Source Or Sink I/O Looks Suspicious

1. Use `list_operations` for the run.
2. Use `get_operation_calls` on the relevant operation.
3. Compare operation status, call status, latency, and error detail.

### Contract Or Schema Behavior Is In Question

1. Use `get_run_contract` to inspect the run-level schema contract.
2. Use `explain_field` for a specific field.
3. Use `list_contract_violations` for producer/consumer contract failures.

### Ad Hoc SQL

Prefer maintained tools when one answers the question. PostgreSQL `query`
supports a restricted live analysis language, enforced by the shared query API
and a qualified PostgreSQL 16 database profile. Install both extras:

```bash
uv sync --frozen --extra mcp --extra postgres
```

Supported forms include canonical Landscape table projections, scalar
expressions and binds, joins, grouping, ordinary non-writing CTEs, and scalar
or EXISTS subqueries. Aggregates are `count`, `sum`, `avg`, `min`, `max`,
`bool_and` and `bool_or`; scalar functions are `lower`, `upper`, `length`, `abs`
and `round`. Derived computed columns require explicit aliases. Unsupported
syntax fails rather than falling back to arbitrary SQL.

Caller-directed locks, settings, notifications and SQL-text evaluators are
excluded, including `pg_advisory_lock`, `set_config`, `pg_notify` and
`query_to_xml`. There are no caller VOLATILE-function exceptions. Recursive
CTEs, set operations, windows, table functions, caller arrays/composite values,
system columns, arbitrary functions/types/collations and explicit noncanonical
schemas are also excluded. SQL text is limited to 64 KiB; PostgreSQL binds must
match the statement exactly and use admitted scalar values. Text in a bind is
data, never an additional statement.

Use a dedicated audit-reader LOGIN with SELECT on the canonical Landscape
tables. Do not use runtime, owner, administrator or `SET ROLE` credentials. The
reader must have no administrative attributes, role memberships, object
ownership, database/schema creation authority or table/column/sequence write
authority. The executing connection is checked on every call; admission is not
cached across pooled connections or profile changes. Existing temporary objects
refuse. The query uses `search_path = pg_catalog, public, pg_temp`, with temporary
resolution explicitly last.

The profile requires the maintained Landscape base-table columns and supported
scalar types, ordinary heap/btree storage, and the reviewed PostgreSQL execution
dependencies. RLS, inheritance, rules, foreign/partitioned relations and
unsupported custom routines, types, casts, operators or index callbacks refuse.
Canonical audit-export integrity triggers remain supported by exact body,
signature and attachment checks; their presence does not authorize invoking
them from caller SQL.

Qualification uses the trusted PostgreSQL 16 distribution with canonical
Landscape initialization. Administrators and the database installation remain
trusted: keep extensions/native preload hooks and catalog/schema maintenance
under operator control and quiesce maintenance during analysis. This contract
does not protect a raw password used outside ELSPETH's query APIs. Maintained
analysis tools retain their ordinary repository connection behavior. Normal
live snapshots/read locks and bounded read contention are accepted; `READ ONLY`
is additional write protection.

The MCP `query` tool executes in a fresh disposable Linux process. It admits
one query per server with no queue: 1 MiB request, 8 MiB complete JSON response,
1 GiB process address space and 30 seconds including cancellation/cleanup.
Over-budget calls fail without truncated rows or fields; other tools remain
available. Unsupported operating systems refuse this MCP query path.
PostgreSQL uses streaming results, a 10-second statement timeout and 1-second
lock timeout. Cancellation targets the child's own connection; the parent
independently kills/reaps and drains the child if needed. These client-process
limits do not cap the remote database's aggregate CPU or memory.
If startup or cleanup cannot finish within its budget, the call reports a
worker error and retains the query slot until its owned cleanup succeeds.

The direct Python `LandscapeAnalyzer.query` facade enforces the same PostgreSQL
syntax/profile restrictions on its actual executing connection. It retains
native values and list-of-dicts results in the trusted caller's process, without
MCP worker isolation, JSON budgets or subprocess cancellation guarantees.
SQLite and SQLCipher retain their file-backed read-only and `query_only`
controls and existing SQL selection behavior.

## Schema Notes

- Landscape uses SQLAlchemy Core table definitions in
  `src/elspeth/core/landscape/schema.py`.
- `nodes` is keyed by `(node_id, run_id)`; joins must include both values when
  joining node records.
- Token outcomes use `completed`, `outcome`, and `path`, not the retired
  `is_terminal` column.
- External calls are parented by either `state_id` or `operation_id`, never both.
- See [Landscape System Architecture](../architecture/landscape.md) for the
  current table grouping and repository layout.
