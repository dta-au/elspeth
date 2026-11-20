# Composer edit battery

Use this runbook to measure whether the Composer can make a long series of
small edits to a very large graph, in a row, through ordinary chat. The
measure is "longest run in a row: K of N". Gold is N of N: every edit
completed, made exactly the change asked for, changed nothing else, used the
expected editing tools, and kept working when the session's history was shed
to fit the context budget.

Written 2026-11-20 for the 0.8.16 release. The tool is
`scripts/staging/composer_edit_battery.py`; the default plan is
`scripts/staging/edit_plans/composer_gold_50.json`.

## What a run does

The battery reads the session's graph and resolves the plan's roles on it.
It then sends each edit as a normal `POST /api/sessions/{id}/messages` turn,
answered by the configured provider, and waits for the turn to finish. It
never writes the graph itself. After every turn it records one JSON verdict
(`edit_result`):

| Check | Meaning |
| --- | --- |
| completed | The operation finished `completed` and at least one provider call succeeded |
| expected change | Each of the edit's expectations holds on the new graph |
| nothing else changed | Every difference from the previous graph is covered by an expectation or the edit's `may_change` list |
| validation | No validation error appeared that was not there before |
| tools | One of the edit's expected tools was used without being rejected, and no forbidden tool (`set_pipeline`) was applied |
| history shed | Whether any call in the turn left out history or stubbed tool results, and what was newly shed |
| timing | Wall, span, provider and server time; provider-call and tool-call counts (below) |

The run ends with an `edit_battery_summary` line carrying
`"line": "longest run in a row: K of N"`, `gold`, the first failing edit,
totals, the server-time share, and how many edits ran with history shed and
how many of those passed. The process exits 0 only on gold.

By default the first failing edit stops the attempt for good, and the attempt
never continues past it. `--keep-going` judges every edit instead, each
against the graph the previous edit actually left, for a full K-of-N
measurement. A failed structural edit usually makes later structural edits
fail too.

## Time and call figures

Each figure comes from what the web API already returns: the public
`llm_call_audit` rows and the assistant tool rows in the audit-grade messages
view. Nothing is estimated; a figure the rows do not carry is `null`.

| Field | Source |
| --- | --- |
| `wall_ms` | Client time from submitting the turn to the poll that saw it finish; resolution is the poll interval |
| `span_ms` | From the turn's first stored row to its last stored row or last provider-call finish |
| `provider_busy_ms` | Union of the turn's provider calls' `started_at`..`finished_at`; overlapping calls count once |
| `provider_latency_ms_sum` | Sum of the calls' `latency_ms` |
| `server_ms` | `span_ms - provider_busy_ms`: time in the turn with no provider call in flight |
| `provider_calls`, `budgeted_provider_calls` | Calls recorded for the turn, and those that carried a context-budget decision |
| `tool_calls_by_name`, `tool_outcomes` | Assistant tool calls by name, with the server-stamped outcome (`applied`, `rejected`, `failed`, `cancelled`, `completed`) |

The summary gives `server_time_share_of_span` (server time over span) and
`non_provider_share_of_wall`.

Not available from the API, so not reported:

- queue wait before the worker started the turn: the operation status
  carries no admission or start timestamp;
- where server time went inside a turn (tool execution, validation,
  persistence): no per-phase timing is exposed;
- advisor time versus planner time when their calls overlap.

## Run it against a local server

Start a local server as in
[Run the Web Composer locally](../guides/web-local-development.md), sign in,
and import or build the large graph in a session. Find the session id with
`GET /api/sessions` or from the session's address in the browser. Then:

```bash
export ELSPETH_BATTERY_PASSWORD='<password>'   # or use --token-env with a bearer token
python -m scripts.staging.composer_edit_battery run \
  --base-url http://127.0.0.1:8451 \
  --session <session-id> \
  --account <username> --password-env ELSPETH_BATTERY_PASSWORD \
  --state-dir <state-dir>
```

That is the dry run: it validates the plan and prints what it would do,
without contacting the server. Add `--execute` to send the edits. Plain
`http` is accepted only for a loopback host. Every deployment value is a flag
or an environment variable (`ELSPETH_OPS_BASE_URL`, `ELSPETH_OPS_SESSION`,
`ELSPETH_OPS_ACCOUNT`, `ELSPETH_OPS_STATE_DIR`); a missing one is a usage
error, never a default. `<state-dir>` must be outside the repository: the
receipts hold graphs and prompt text.

Other actions:

```bash
python -m scripts.staging.composer_edit_battery plan [--plan <file>]
python -m scripts.staging.composer_edit_battery report --session <session-id> --state-dir <state-dir> [--attempt <n>]
```

`plan` lists the edits and the tools they expect. `report` prints the saved
verdicts and summary of an attempt without contacting the server.

## Fitting the plan to the graph

The default plan names four roles, resolved when an attempt starts:

| Role | Resolved to |
| --- | --- |
| `source` | The first source |
| `head` | The single node fed by that source |
| `llm_inline` | The first transform whose `prompt_template` is inline text |
| `llm_file` | The first transform whose `prompt_template` is bound to a prompt file |

When a graph has no component for a role, the battery stops before sending
anything and names the role. Name a component with `--target ROLE=ID`, or
write a plan for that graph. The roles and resolved messages are saved with
the attempt, and a resumed attempt reuses them.

The default plan's fifty edits cover:
- pipeline name and description (`set_metadata`);
- inserting, describing, reordering, renaming and removing temporary
  passthrough steps between the source and its head (`splice_transform`,
  `upsert_node`, `remove_node`);
- a temporary error output and error routing (`set_output`,
  `patch_output_options`, `remove_output`);
- LLM option patches and their restoration (`patch_node_options`);
- inline prompt sentence edits (`change_substring`);
- prompt-file edits (a new file, re-pointed with `wire_blob_inline_ref`);
- restoring each changed name and description.

It does not change a source's options, because no source option is common to
every source plugin. Add such an edit in a plan written for the graph.

### Plan format

A plan is JSON with `format` `elspeth-composer-edit-plan/1`, a `name`,
`forbidden_tools`, `targets` (role name to resolver) and `edits`. Each edit has:
- an `id`;
- the chat `message`;
- `expect`, a list of expectations;
- optionally `may_change` (pointer prefixes allowed to change),
  `tools_any` (at least one must be used) and `approve` (below).

Pointers are JSON pointers into the graph view: `sources`, `nodes` keyed by
id, `outputs` keyed by name, `metadata` and `edges` keyed by content.
`{role}` fills in a resolved role. `{was:/pointer}` fills in the value at the
start of the attempt. The expectation forms are listed in the docstring of
`scripts/staging/edit_plan.py`:
- `equals`, `ends_with`, `present`, `absent`;
- `restored`;
- `chain`;
- `text` with `append`, `replace` or `remove`.

## Prompt review cards

A prompt change opens a review card that the user must approve before the
graph validates. An edit with `"approve": {"kind": "llm_prompt_template",
"node": "{role}"}` lets the battery act as that user. It does so only after
the edit's expected change has verified, only for a card on the named node,
and only when the card shows exactly the node's current prompt text and
files. It then resolves the card `accepted_as_drafted` through
`POST .../interpretations/{id}/resolve`. This exercises approval authority on
the signed-in account: run the battery only on a session made for testing.

## Safety properties and resuming

- Every edit is an ordinary chat message; the battery never writes graph
  state.
- Each edit's operation id is saved before it is submitted. A resumed run
  polls that operation instead of submitting again.
- An attempt resumes only from a checkpoint for the same session, attempt and
  plan file (by SHA-256).
- Before each new edit, the live graph must equal the graph the previous edit
  left. A change made outside the battery stops the attempt.
- A stopped attempt never continues. Inspect the per-edit receipts
  (`edit-battery-<attempt>-<session>-edit-<n>.json` in `<state-dir>`), then
  start a new `--attempt`.

Each test is named in `tests/unit/scripts/staging/test_composer_edit_battery.py`.

Load on the server: per edit the battery makes two state reads (before and
after, plus one after an approval), one turn submission, polls no more often than
`--poll-seconds` (default 2) or the server's `poll_after_ms`, and one read of
the message rows added since the previous edit. That read uses the
audit-grade messages view, which records an access entry each time.
