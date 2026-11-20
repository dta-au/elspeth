# Composer edit battery

Use this runbook to measure whether the Composer can make a long series of
small edits to a very large graph, in a row, through ordinary chat. The
measure is "longest run in a row: K of N". Gold is a complete attempt: every
applicable edit judged and passed in one attempt, with at least 50 applicable
edits, that leaves the graph as it found it. Each passing edit completed,
made exactly the change asked for, changed nothing else, used one of its
acceptable editing tools, and kept working when the session's history was
shed to fit the context budget. An incomplete attempt, an attempt with any
failed edit (`--keep-going` included), a plan with fewer than 50 applicable
edits, or an attempt that leaves any difference in the graph is never gold.

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
  and `options.approved_prompt_artifact_hash`, and a source's
  `options.interpretation_requirements` and `options.source_authoring`, but
  only on a component the edit names (in an expectation pointer, a chain, a
  new or renamed step, `may_change` or `edges_touching`) or on the node whose
  review card the edit approves. The same field changed on any other
  component is an unrelated change and fails the edit;
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
text, and server-written fields are ignored). Gold needs it to be empty. It
also reports `files_left_in_session`: each file in the session that was not
there when the attempt started, by id and filename, with `referenced` saying
whether the final graph still uses it. Prompt-file rounds leave such files
behind; `cleanup` can delete the unused ones. The process exits 0 only on
gold.

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
| `tool_timings` | Each tool call with a timed invocation record on its tool row: tool name, status and `latency_ms` only |
| `tool_timings_complete`, `tool_busy_ms` | Whether every tool call had a timed record; if so, the union of their `started_at`..`finished_at`, else `null` |
| `server_ms_outside_provider_and_tools` | `span_ms` less the union of provider and tool intervals; `null` unless both are known |
| `calls` | Each provider call, sanitised: the usage, latency, finish-reason, model-label and cost fields, `request_byte_split` (message, tools and total bytes) and the numeric `context_budget` fields with the shed history indices and a count of stubbed tool results |
| `end_reviews` | Each END-phase advisor checkpoint record the view returned: `phase`, `verdict`, `publication_verdict`, `source` |

The summary gives `server_time_share_of_span` (server time over span),
`non_provider_share_of_wall`, the totals of `tool_busy_ms` and
`server_ms_outside_provider_and_tools` (`null` if any edit lacks them),
`edits_with_complete_tool_timings` and `end_reviews_recorded`.

The sanitising rule for `calls` is the customer's: a text field is kept only
when it is a short token of letters, digits and `_ . / : -` that is not a
path or a URL; any other value drops the field. No prompt or reply text,
tool argument or result, hash, request id, call id or tool-call id is ever
written to the checkpoint.

Not available from the API, so not reported:

- queue wait before the worker started the turn: the operation status
  carries no admission or start timestamp;
- where server time went inside a turn (validation, persistence, advisor
  work): no per-phase timing is exposed;
- tool time on current servers: a primary tool row carries only a visible
  companion record with no timing, so `tool_timings_complete` is false and
  `tool_busy_ms` null; `server_ms` then still bounds all non-provider time;
- advisor end reviews on current servers: the messages view returns
  only provider-call and planner-attempt audit rows, so `end_reviews` is
  empty (see the structural-cycle workload below);
- advisor time versus planner time when their calls overlap.

## Structural-cycle workload

`--workload structural-cycle` sends the customer's large-graph stress
workload instead of a plan file, and judges it with their verifier and error
codes. It repeats a ten-step cycle around an anchor step: add two passthrough
nodes before it and two after it, swap each pair, remove all four, and on the
tenth step restore the original route names. Cycle 1 names its nodes
`compact_*`, later cycles `cycle1_*`, `cycle2_*` and so on. The wording of
every step is the customer's, pinned by a test; do not edit it.

A *position* is where a cycle edits: an upstream (a source or step), the
anchor step, and a downstream (a step or a sink). With `--fixture` the cycles
rotate through the positions in the fixture's manifest, so the edits land in
different places of the graph. Without it there is one position, the
customer's: `--source` → `--anchor` → `--sink` (defaults `source`,
`capacity_probe`, `results`). Giving both is refused.

```bash
python -m scripts.staging.composer_edit_battery plan --workload structural-cycle --steps 50 --fixture "$FIXTURE"
python -m scripts.staging.composer_edit_battery run --workload structural-cycle --steps 50 --fixture "$FIXTURE" \
  --base-url "$URL" --session "$SESSION" --account "$ACCOUNT" --state-dir "$STATE" --execute
```

For a downstream sink the wording says "the `<name>` sink", as the
customer's does; a downstream step is just named. The `plan` action prints
the tenth step's restore values as `{was:/nodes/<anchor>/...}` fields; `run`
fills them from the graph as JSON text, as the customer's runner does.

`--steps` is 1 to 60 (default 30). The step count and positions are part of
the attempt's identity: resuming with others is refused.

**The run changes the test graph.** Every step is a real, auto-committed
edit. Run it only on a session made for the run (see
[Large-graph fixture](#large-graph-fixture)), never on a graph anyone uses.
A complete cycle ends with the graph's original wiring; an attempt stopped
mid-cycle leaves passthrough nodes in place. `cleanup` does not apply to a
workload: set the fixture up again in a new session.

### What the battery checks before the first step

- each position's upstream routes straight into its anchor (a transform), and
  the anchor straight into its downstream, which is a sink exactly when the
  position says so;
- no node already has a name a cycle adds;
- the graph is valid, with its prompt and file reviews resolved, and the
  session is in auto-commit.

The summary reports the graph's counts as `baseline_graph_counts` (sources,
nodes, LLM nodes, sinks, prompt queries), and each step's result its
`graph_counts`.

### How a step is judged

Each step must pass all of these. The codes are the customer's.

| Code | Fails when |
| --- | --- |
| `node_inventory_mismatch` | The node ids are not the starting graph's plus exactly the passthroughs the step should leave |
| `original_llm_content_changed` | A starting node changed beyond the wiring the step had to touch: the upstream's `on_success`, the anchor's `input` and `on_success`, a downstream step's `input`. The customer's code; it covers every starting node, not only the LLM one |
| `source_content_changed` | A source changed (beyond the upstream source's `on_success`), or a source was added or removed |
| `sink_content_changed` | Any output changed |
| `metadata_changed` | The graph metadata changed |
| `routing_mismatch` | The chain from the upstream does not visit the step's nodes in order |
| `unexpected_auxiliary_plugin` | A node other than the anchor in the chain is not a passthrough transform |
| `routing_does_not_reach_sink` | The chain does not end at the downstream (the sink, or the downstream step's `input`) |
| `baseline_routing_not_restored` | At the end of a cycle, the original route names around the anchor are not back |
| `runtime_validation_failed` | The graph is not valid |
| `operation_not_completed` | The turn did not complete |
| `operation_result_unavailable` | The completed turn has no result |
| `publication_rejected` | The reply was rejected at publication |
| `no_published_reply` | The reply has no non-blank text |
| `additional_system_notice_requires_review` | The reply carries a trusted system notice other than the checkpoint save status |
| `end_review_not_clean_and_admitted` | The turn's last end review is not verdict `clean` with publication verdict `ADMIT` |

The battery also keeps two of its own findings: `evidence_not_settled_*`
(the turn's audit rows never settled) and `no_successful_provider_call`. The
second holds the rule that the LLM does the job: a step the provider took no
part in is never a pass. Plan expectations, unrelated-change and tool-call
findings do not apply to the workload; the verifier's state check covers the
whole graph instead.

Plan-file edits never required an end review. They required a completed
turn, a successful provider call, settled evidence, the plan's expectations,
no unrelated change, no new validation error and no refused tool call.

The customer's runner read each turn's end review from the database. No
API route returns it today: the END-phase `advisor_checkpoint_pass_audit`
rows are stored with the session's messages, but the audit-grade messages
view admits only provider-call and planner-attempt rows, and no other route
(state, composer progress, operation status, diagnostic evidence, export)
carries the verdict. `composer_meta.completion_gates` on the state shows a
blocking advisor gate, never a pass, so it cannot tell a clean review from
none.

So `--end-review-evidence if-recorded` is the default: a recorded review
that is not clean and admitted fails the step, and a step with none gets
the warning `end_review_not_recorded`. The summary's `end_reviews.line`
says in words how many steps were verified; with none it reads
"end reviews: none exposed by this server; not verified", and
`end_reviews.verified` is false. `--end-review-evidence required` (the
customer's rule) fails a step with no recorded review, which on today's
servers is every step. Report which mode a run used, and quote the line.

The first failing step stops the attempt; `--keep-going` judges every step.
A workload run exits 0 when every requested step passed. The summary still
reports `gold` (50 or more steps, all passed, fixture as found).

## Large-graph fixture

`scripts/staging/large_graph_fixture.py` writes a synthetic large graph for
the workload, and the battery's `setup` action creates it in a session. All
names, fields and text are made up from a seed; the same seed and arguments
give the same bytes.

The `acceptance` preset is wired like a real assessment graph rather than one
big LLM block: a CSV source with a fixed schema of 14 fields, bound to an
uploaded file, with validation failures routed to the error sink; a
`file_inject` step with file-held content; two case-study chains (an
assessment LLM step feeding a multi-query scoring step); evidence checks, a
cross-case comparison, risk and equity reviews and a larger multi-query
overall rubric; a gate that routes low scores to a moderation-queue sink;
summary and feedback LLM steps; truncate steps capping case text before the
steps that read it; and a final `field_mapper` into a main sink that
declares its fields. Every LLM step declares `required_input_fields` that
earlier steps produce, and its prompts name those fields. All prompt text is
file-held: single-prompt steps hold a system and a user prompt in files;
multi-query steps hold their rubric (system prompt) and every query template
in files, each template bound to its designed slot
`node:<step>.options.queries.<query>.template` and left out of the YAML.

| Default (acceptance) | Value |
| --- | --- |
| Steps (transforms and gates) | 27 |
| LLM steps | 12 (4 multi-query: 3, 3, 3 and 8 queries) |
| Prompt files | 39, holding exactly 200,000 bytes: 16 single-step prompts (1.7-2.4 KB), 17 query templates (4.5-6.5 KB), 4 rubrics (11-18 KB), 2 injected files (7-13 KB) |
| Sinks | 3 (main, error, moderation queue) |
| Graph document once imported | about 38 KB: the prompt text stays in the bound files |
| Positions | after the source, mid-chain between two LLM steps, at the final mapper |

`--steps`, `--llm-steps`, `--queries-per-llm-step` (the 3 scoring steps),
`--large-queries` (the overall rubric), `--total-prompt-bytes`,
`--inject-files` and `--seed` change the size; the counts and bytes are met
exactly. `--query-templates inline` writes the query templates into the
YAML instead (short ones, for comparison); `--file-inject omit` leaves the
`file_inject` step out. `--profile` names the LLM profile the steps use on
the target server (default `task-role`). The generator refuses a prompt file
over the server's per-file cap (256 KiB, read from `core/blobs_inline.py`).

The `single-node` preset reproduces the customer's shape for a like-for-like
comparison with their telemetry: source `source`, one LLM node
`capacity_probe` with 32 file-held query templates of 15,600 bytes
(499,200 bytes), a 4,000-byte system prompt file, and sinks `results` and
`errors`. `--queries-per-llm-step` and `--total-prompt-bytes` change the
count and the size (the system prompt stays 4,000 bytes; the rest is split
evenly over the queries). With `--query-templates inline` the templates are
written into the YAML as literal blocks: 518,741 characters, 5,547 under the
YAML import limit of 524,288.

### Limits a large graph meets (measured on candidate 5)

**Aggregate file-held text: 1 MiB.** The text bound to a graph's files may
total at most 1,048,576 bytes (`BLOB_INLINE_AGGREGATE_BYTE_CAP`). Measured
through the real import:

| Fixture | Files | Bytes | Result |
| --- | --- | --- | --- |
| `--preset acceptance --total-prompt-bytes 943718` (0.9 MiB) | 39 | 943,718 | imports, valid |
| `--preset acceptance --total-prompt-bytes 1153433` (1.1 MiB) | 39 | 1,153,433 | HTTP 500 at import |

Over the cap, the import's prompt-review snapshot raises
`PromptFileTooLargeError`, which no handler maps: the user gets an internal
server error, not the size refusal, and no graph is saved.

**State scan: 4,096 values.** The server scans the whole state for
credential material and refuses one whose scan visits more than 4,096
values (`CredentialTraversalPolicy.max_nodes`). The manifest's
`state_scan_values_at_import` is that count for the graph as the YAML import
scans it, measured with the server's own importer and scan. A file-held
template's marker is bound after that scan, so the stored state counts more:

| Fixture (single-node) | Values at import | Values stored | Result |
| --- | --- | --- | --- |
| 251 file-held queries | 3,080 | 4,092 | imports, valid, readable |
| 252 file-held queries | 3,092 | 4,108 | imports; `GET /state` then answers 409 "The saved graph is unavailable" |
| 309 inline queries (400 B) | 4,085 | 4,094 | imports, valid |
| 310 inline queries (400 B) | 4,098 | - | HTTP 422 at import |

In this generator's query shape a file-held query adds 16 stored values and
an inline one 13. At 252 file-held queries the import accepts the graph and
the session's graph can no longer be opened: the read path's editable-draft
check fails it for credential material (`checkpoint_credential_material`).
The inline refusal at 310 is `credential_material_rejected` with category
`traversal_limit`, and its text tells the user the content "appears to
contain a credential".

### `file_inject` content

The default `--file-inject file` holds the `file_inject` content in uploaded
files. It validates on servers with the `file_inject` producer-guarantee fix
(candidate 5 onward); on older servers authoring validation reports
"Producer guarantees: (none)" for every later step. `--file-inject omit`
leaves the step out for those servers. The composer refuses `file_inject`
content written into the YAML, so there is no inline form.

### Creating the session

```bash
python -m scripts.staging.large_graph_fixture --preset acceptance --out "$FIXTURE"
python -m scripts.staging.composer_edit_battery setup --fixture "$FIXTURE" \
  --base-url "$URL" --session "$NEW_SESSION" --account "$ACCOUNT" --execute
```

`--out` must be an empty directory outside the repository. Create a new,
empty session in the Composer first; `setup` refuses a session that already
has a graph. With `--execute` it uploads the source data and prompt files,
imports `pipeline.yaml` with their bindings (`POST /state/yaml`, as a person
importing YAML does), resolves each prompt review card as accepted only after
checking that it belongs to one of the fixture's LLM steps and shows exactly
the text uploaded for that step, and requires the graph to be valid with no
review pending. A review of any other kind is left for you and setup fails.
Without `--execute` it prints what it would upload and contacts nothing.

The fixture directory's `manifest.json` lists every file with its digest, the
positions and the measured sizes; the battery refuses a fixture whose files
no longer match it.

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
  --account <username> --password-env ELSPETH_BATTERY_PASSWORD --state-dir <state-dir> \
  --attempt <attempt> [--plan <plan>] [--execute]
```

Without `--execute`, `cleanup` makes no network call at all: it prints what
it would do (from the saved attempt in `<state-dir>`, if there is one) and
exits 0. Pass the `--attempt` and `--plan` the attempt ran with so it finds
that attempt.

With `--execute` it asks the Composer, one ordinary chat turn at a time, to
remove what a battery attempt left, in this order: `battery_*` outputs,
`battery_*` steps (reconnecting what fed each one to what it fed), battery
marker sentences in prompts, the pipeline name, then files the graph no
longer uses. Every cleanup turn is judged like a battery edit: it must
complete with a successful provider call, make exactly the removal asked for,
change nothing else, add no validation error and use an acceptable tool. A
marker removal opens a prompt review card; cleanup approves it only on a
clean verdict and only when the card shows the node's current prompt, as a
battery edit does. The first turn whose verdict is not clean stops the
cleanup.

Two things need the saved attempt (same `--state-dir`, `--attempt`, `--plan`
and session). The pipeline name is put back to the name in the attempt's
starting graph; without the attempt, a battery pipeline name is only reported.
Files are deleted when they were added since the attempt started and no step
uses them; without the attempt, only unused files named `battery_*` are.

Cleanup ends with a `cleanup_result` line: the number of turns, the failed
turn (if any), the leftovers still on the graph, files still left, and any
review card that is still pending and was not there before the cleanup
began. It exits 0 only when the cleanup turns all passed and all of those
lists are empty. Check the cleanup turns in the Composer before starting a new
attempt.

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
