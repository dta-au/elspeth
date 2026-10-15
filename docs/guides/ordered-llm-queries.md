# Ordered LLM queries

Use `query_input_mode: ordered` when one LLM node needs a later query to read
an earlier query's validated result. The default `upstream` mode continues to
give every query the original input row.

Ordered mode requires list-form queries with unique names and `pool_size: 1`.
For example, these LLM options extract a score and then explain it:

```yaml
query_input_mode: ordered
pool_size: 1
schema:
  mode: observed
required_input_fields: [text]
response_field: answer
queries:
  - name: extract
    input_fields: {text: text}
    template: 'Return the score stated in {{ row.text }}.'
    response_format: structured
    output_fields:
      - {suffix: score, type: integer}
  - name: explain
    input_fields: {score: extract_score, text: text}
    template: 'Explain score {{ row.score }} using {{ row.text }}.'
```

Add the provider and deployment options required by your environment. Each
query still makes a provider call. Earlier outputs are available only through
explicit `input_fields` bindings to their exact generated names. Self/forward
references and generated-field collisions are rejected before execution.
Generated fields must not be declared as upstream inputs.

Structured numbers retain the existing normalization: integer output `5.0`
becomes integer `5`, and number output `7` becomes float `7.0`. Boolean values
cannot substitute for integers; nonintegral numbers and null fail an integer
declaration before a dependent call. Structured nullable outputs are unsupported.
Effective declared output types are checked before the next query renders.

`row.source_row` and image inputs always read the original entry row. Existing
projection meanings remain: a nonempty `required_input_fields` list bounds
visibility; explicit `[]` exposes the whole original row. A constant first
ordered query can use `input_fields: {}` without a fabricated external input.

The node emits one complete row after all queries succeed. A terminal failure
discards intermediate row updates and routes the original entry row through
the node's error policy. Earlier calls and their charges remain in the audit
trail. Capacity retries keep earlier successful work in memory and retry the
current query under the existing shared retry budget. This is not a hard total
invocation deadline.

Recovery remains at the node boundary and can repeat earlier calls. There is no
durable mid-node resume or exactly-once provider guarantee. Semantic LLM calls,
including retries and usage, remain attached to the actual node state. A call
success proves that a response was recorded, not that its output was accepted
or published; interrupted prefixes can leave acceptance unknown. Replay keeps
its existing fail-closed response/error admission rules.

## Choosing a combined step in Composer

The planner may offer a combined step for an eligible linear chain. Independent
queries can already use upstream mode; dependent queries need ordered mode.
Review all consumers, exact generated names/types, rendered prompts, effective
provider/profile/settings, and any intermediate shield, mapper, gate, branch,
sink or conditional call. Incompatible settings or intermediate boundaries
require retaining separate steps.

Accepting compaction changes failure payloads, audit grouping and checkpoint
identity. It requires a new pipeline version/run and cannot resume an old
checkpoint. Fewer graph nodes do not establish fewer model calls or tokens.
The provider planner authors the proposal through ordinary tools, with prompt
and interpretation review retained. Declining leaves graph and settings unchanged.
