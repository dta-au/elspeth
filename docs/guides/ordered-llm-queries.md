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

To preserve an exact structured output name, set a query's
`structured_output_prefix: ""`: `{suffix: answer, type: string}` then publishes
`answer`. A nonempty ASCII identifier of at most 64 characters publishes
`<stem>_answer`; omitted or null keeps `<query_name>_answer`. Provider JSON keys
remain the suffixes. Raw replies and usage/model fields retain their query-name
prefixes. This option requires 0.8.9 and applies only to multi-query transforms.
Any non-null override activates complete generated-name collision refusal across
all queries, even in upstream mode. Null preserves upstream mode's legacy
overwrite rules; ordered mode always refuses collisions.

For a separate semantic review, bind a later query's `input_fields` to the actual
validated writer outputs and relevant original evidence. Give the review a real
rejection value, for example `{suffix: admit, type: boolean}`, and check
`row['review_admit']` in `result_checks` when its query is named `review` with
the default prefix. False then rejects before downstream release. Both effective
prompts and authored judgment semantics retain ordinary review. A second request
to the same model is a separate check, not a semantic correctness guarantee.

Structured numbers retain the existing normalization: integer output `5.0`
becomes integer `5`, and number output `7` becomes float `7.0`. Boolean values
cannot substitute for integers; nonintegral numbers fail an integer declaration
before a dependent call. Null also fails unless that output descriptor sets
`nullable: true`. The key remains required, and a later query or expression must
handle the possible null explicitly. See the
[output-field reference](../reference/configuration.md#multi-query-queries).
Effective declared output types are checked before the next query renders.

A separate downstream LLM node can also require a nullable upstream field.
Presence and nullability are distinct: a present `None` reaches the normal
template and checks only when its incoming owned field contract permits null.
An absent field, nonnullable null, uncontracted extra null or blank string does
not gain that permission. Keep the required binding and handle the allowed null
in the prompt/checks; do not replace it with zero or an invented sentinel.

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

## Checking the completed result

Use optional `input_checks` for admission on the actual incoming row before any
per-row writer or reviewer request. They use the same named expression shape and
strict Boolean behavior as `result_checks` below. For example:

```yaml
input_checks:
  - name: source_admitted
    expression: "row['admitted']"
```

A rejection routes the entry row through `on_error` with a static input-check
category. It does not widen prompt visibility. Operation startup preflight may
still call the provider; this option suppresses rejected **row** requests.
Keep any detailed admission causes in the incoming row so a required CSV error
sink's `row_json` retains them. The error text itself names the failed check.
Omitted/null checks preserve existing behavior; each supplied input/result list
independently requires 1–64 uniquely named checks.

Single-query and multi-query LLM transforms support optional `result_checks`.
Each named expression evaluates the actual completed output row, after typed
extraction and after all queries finish, before downstream release:

```yaml
result_checks:
  - name: positive_score
    expression: "row['extract_score'] > 0"
```

Every expression must return the Boolean `True`. A false result, a non-Boolean
value such as `1`, or an expression evaluation error rejects the row through
the transform's `on_error` destination. The nonretryable error records the
configured check name and a static category, without copying row or model
content into the cause. Existing provider requests, responses and usage remain
in the audit trail; a rejected assembled output row is not emitted. A readable
CSV error sink can retain that cause under the
[required-row retention configuration](../runbooks/csv-required-row-retention.md).

Expressions use the restricted gate grammar: comparisons, arithmetic, membership,
`len`, `abs`, `lower`, `upper`, `strip`, `casefold`, `starts_with`, `word_count`
and `decimal_within` ([expression reference](../reference/configuration.md#expression-syntax)). Python string methods,
slices and arbitrary function calls are unavailable. Checks provide mechanical
postconditions; semantic correctness still needs an appropriate review mechanism.

Keep `required_input_fields` limited to fields the prompt may see. Generated
fields checked here are outputs, not upstream requirements. An upstream field
needed only by a local check can be required with `schema.required_fields`
without exposing it to the prompt. Checks may inspect optional or dynamically
selected final-row fields; an unhandled missing key fails the check.

Omitted or null `result_checks` preserves existing behavior. An explicit list
must contain 1–64 checks with distinct lowercase identifiers, at most 64
characters each, and expressions of at most 4096 characters. Unsafe syntax is
rejected at configuration time before provider startup. Existing node-boundary
recovery rules still apply; checks do not add durable mid-node resume.

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
