# Composer edit battery

Use this runbook to measure whether the Composer can make a long series of
small edits to a very large graph, in a row, through ordinary chat. The
measure is "longest run in a row: K of N". Gold is a complete attempt: every
applicable edit judged and passed in one attempt, with at least 50 applicable
edits. Each passing edit completed, made exactly the change asked for,
changed nothing else, used one of its acceptable editing tools, and kept
working when the session's history was shed to fit the context budget. An
incomplete attempt, or a plan with fewer than 50 applicable edits, is never
gold.

Written 2026-11-20 for the 0.8.16 release. The tool is
`scripts/staging/composer_edit_battery.py`. Two plans ship in
`scripts/staging/edit_plans/`:

| Plan | For | Edits |
| --- | --- | --- |
| `composer_gold_50.json` (default) | A graph whose prompts are all held in files | 50 always applicable, plus 4 optional |
| `composer_mixed_50.json` | A graph with an inline prompt and a prompt in a file | 50 always applicable, plus 4 optional |

Both plans leave the graph as found.

## Before you start

- The session must be in `auto_commit` trust mode. In `explicit_approve`
  each edit becomes a proposal waiting for the user to accept it. The battery
  does not accept proposals on the user's behalf, so it refuses to start and
  says so. Switch the session to auto-commit in the Composer first.
- The graph must hold no leftovers of an earlier attempt: no `battery_*`
  step or output, no battery marker sentence in a prompt, and no pipeline
  name starting "Battery check". A new attempt refuses to start on such a
  graph and names what it found. Use `cleanup` (below).

## What a run does

The battery reads the session's graph and resolves the plan's roles on it.
Before sending each edit it checks that none of the edit's expectations
already holds on the live graph. An edit whose change already holds is a plan
or precondition error: it is recorded as a failure (`not_sent`) and never
sent. Otherwise it sends the edit as a normal
`POST /api/sessions/{id}/messages` turn, answered by the configured provider,
and waits for the turn to finish. It never writes the graph itself.

A turn's evidence rows can be stored after the operation reports
`completed`. The battery therefore re-reads the turn's rows, `--poll-seconds`
apart and at most six times, until two reads agree and the provider audit
row is present. Evidence that does not settle within that bound fails the
edit (`evidence_not_settled_after_6_reads`).

After every turn it records one JSON verdict (`edit_result`):

| Check | Meaning |
| --- | --- |
| completed | The operation finished `completed` and at least one provider call succeeded |
| expected change | Each of the edit's expectations holds on the new graph |
| nothing else changed | Every difference from the previous graph is covered by an expectation, the edit's `may_change` prefixes, or edges touching its `edges_touching` components. Fields the server writes itself are allowed by name and listed in `server_written` (below) |
| validation | No validation error appeared that was not there before |
| tools | At least one of the edit's `tools_any` and every one of its `tools_all` was used. A forbidden tool (`set_pipeline`) called and not rejected fails the edit, whatever its stamp |
| warnings | Tool calls the server rejected inside the turn. On a passing edit they were corrected; they do not fail it |
| history shed | Whether any call in the turn left out history or stubbed tool results, and what was newly shed |
| timing | Wall, span, provider and server time; provider-call and tool-call counts (below) |

Fields the server writes, allowed by name and reported as `server_written`:

- a node's `options.interpretation_requirements` (review requirement rows)
  and `options.approved_prompt_artifact_hash`;
- a source's `options.interpretation_requirements` and
  `options.source_authoring`;
- a deployment-required control the server auto-wired into the turn's
  graph (a new node whose requirement row carries `required_control_auto_wired`),
  its edges and the routes around it. A `chain` expectation steps through such a
  control.

The graph is compared by content, not by position where order carries no
meaning: nodes by id, outputs by name, edges by their endpoints, type and
label, and any list whose items all carry a distinct `id` (review rows, for
example) by that id. A description that is absent, null or empty text counts
as "no description"; the text `"null"` is a description.

The run ends with an `edit_battery_summary` line carrying
`"line": "longest run in a row: K of N"`, `complete`, `gold`, the first
failing edit, the skipped edits, `tool_calls_rejected_then_corrected`, the
count of server-written changes, `tool_coverage` (below), totals, the
server-time share, and how many edits ran with history shed and how many of
those passed. A complete attempt also reports `as_found_differences`: what
still differs from the graph the attempt started on (prompts are compared by
text). The process exits 0 only on gold.

`tool_coverage` lists the editing tools used at least once against the 22
editing tools the Composer offers. The list is `EDITING_TOOLS` in the
battery, and a test pins it to the tool registry.

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
| `server_ms` | `span_ms - provider_busy_ms`: time in the turn with no provider call in flight. `null`, never negative, when the span is shorter than the provider time (a call that began before the turn's first stored row) |
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

`plan` lists the edits, their roles and the tools they accept, with the
plan's tool coverage. `report` prints the saved verdicts and summary of an
attempt without contacting the server.

### Leftovers and cleanup

```bash
python -m scripts.staging.composer_edit_battery cleanup \
  --base-url http://127.0.0.1:8451 --session <session-id> \
  --account <username> --password-env ELSPETH_BATTERY_PASSWORD --state-dir <state-dir> [--execute]
```

Without `--execute`, `cleanup` signs in, reads the graph, reports any
leftovers and the chat messages it would send, and sends nothing. It exits 1
when there are leftovers. With `--execute` it asks the Composer, in ordinary
chat turns, to remove leftover outputs, then leftover steps (reconnecting the
flow around them), then battery marker sentences in prompts. It then reports
what remains and exits 0 only when nothing does. A leftover pipeline name is
reported only: the battery cannot know the name it replaced. Check every
cleanup turn's change in the Composer before starting a new attempt.

## Fitting the plan to the graph

The plans name these roles, resolved when an attempt starts:

| Role | Resolved to | Required |
| --- | --- | --- |
| `source` | The first source | yes |
| `head` | The single node fed by that source | yes |
| `llm_file` | The first transform whose `prompt_template` is bound to a prompt file | yes |
| `llm_inline` | The first transform whose `prompt_template` is inline text | mixed plan only |
| `output` | The first output | yes |
| `csv_source` | A `csv` source with no explicit `skip_rows` option | optional |
| `secret_node`, `secret_option`, `secret_name` | The first node with an option holding a secret reference, that option, and the secret it names | optional |
| `spare_secret` | A second secret the operator names with `--target spare_secret=NAME` | optional |

When a graph has no component for a required role, the battery stops before
sending anything and names the role. Name a component with
`--target ROLE=ID`, or use a plan that fits the graph. An edit whose optional
role is absent is skipped as not applicable: it is reported in
`skipped_not_applicable`, is not judged, and neither extends nor breaks the
run in a row. The roles and resolved messages are saved with the attempt, and
a resumed attempt reuses them.

The edits cover, with each edit's acceptable tools:
- pipeline name and description (`set_metadata`);
- inserting, describing, reordering, renaming and removing temporary
  passthrough steps between the source and its head (`splice_transform`,
  `upsert_node`, `remove_node`). A new step is checked field by field:
  plugin, node type, error routing, only the expected option keys, and every
  other field unset. A rename must keep every field but the id and routing;
- temporary outputs, their path and description, and error routing to them
  and away again (`set_output`, `patch_output_options`, `upsert_edge`,
  `remove_edge`, `remove_output`). Edge changes are allowed only on edges
  touching the named components;
- LLM option patches, a step description, and their restoration
  (`patch_node_options`, `upsert_node`);
- a new prompt file holding the step's prompt with a sentence added, which
  must use both `create_blob` and `wire_blob_inline_ref`, then small changes
  to that file-held prompt (`wire_blob_inline_ref`; the Composer cannot change
  a bound file's bytes, so it writes a new file and re-points the step, and
  the verdict records the tools it used);
- in the mixed plan, inline prompt sentence edits (`change_substring`,
  `patch_node_options`);
- optional: a source option set and removed (`patch_source_options`,
  `set_source`), and a secret reference switched to the spare secret and back
  (`wire_secret_ref`);
- restoring each changed name and description.

No edit message names a tool. Run `plan` to see each plan's tool-coverage
table.

### Plan format

A plan is JSON with `format` `elspeth-composer-edit-plan/2`, a `name`,
`forbidden_tools`, `targets` (role name to resolver; `"optional": true` makes
a role optional) and `edits`. Each edit has:
- an `id`;
- the chat `message`, which must not name a tool;
- `expect`, a list of expectations;
- `tools_any` (at least one must be used) and/or `tools_all` (every one must
  be used);
- optionally `may_change` (specific pointer prefixes allowed to change, never
  a whole collection), `edges_touching` (components whose edges may change)
  and `approve` (below).

Pointers are JSON pointers into the graph view: `sources`, `nodes` keyed by
id, `outputs` keyed by name, `metadata` and `edges` keyed by content.
`{role}` fills in a resolved role. `{was:/pointer}` fills in the value at the
start of the attempt. The expectation forms are listed in the docstring of
`scripts/staging/edit_plan.py`:
- `equals`, `ends_with`, `present`, `absent`;
- `restored`;
- `chain`;
- `text` with `append`, `replace` or `remove`;
- `new_node`, `new_output` and `renamed_node`.

## Prompt review cards

A prompt change opens a review card that the user must approve before the
graph validates. An edit with `"approve": {"kind": "llm_prompt_template",
"node": "{role}"}` lets the battery act as that user. It does so only on a
turn whose verdict is otherwise clean (expectations hold, nothing unexpected
changed, no new validation error, no tool finding), only for a card on the
named node, and only when the card shows exactly the node's current prompt
text and files. It then resolves the card `accepted_as_drafted` through
`POST .../interpretations/{id}/resolve`. The resolve writes the node's
`approved_prompt_artifact_hash` and settles its requirement row; those are
server-written. Any other change the approval makes fails the edit
(`approval_changed:`). This exercises approval authority on the signed-in
account: run the battery only on a session made for testing.

## Safety properties and resuming

- Every edit is an ordinary chat message; the battery never writes graph
  state.
- It refuses a session in `explicit_approve` trust mode, and a new attempt on
  a graph holding leftovers.
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
after, plus one after an approval), one turn submission, and polls no more
often than `--poll-seconds` (default 2) or the server's `poll_after_ms`. It
reads the message rows added since the previous edit at least twice and at
most six times. Those reads use the audit-grade messages view, which records
an access entry each time. Each attempt also reads the trust mode once.
