# Canonical Pipeline Capabilities

This is ELSPETH's static, public pipeline-language contract. It describes what
every authoring surface can author; live discovery remains the authority for
which plugins and models are installed and authorized in the current
deployment. Every authoring surface receives these exact bytes before its
interaction rules.

## Canonical proposal and discovery

[capability:discovery-order]

Author pipelines as one complete canonical pipeline document — the
`set_pipeline` object shape. Each authoring surface accepts that document
through its own terminal authoring tool; your interaction rules and
per-request instructions name yours. Use the read-only discovery tools before
authoring:

1. Read the request's information manifest first. Its current pipeline
   projection and policy snapshot are already supplied facts; do not repeat a
   state or catalog read that the manifest says is closed.
2. Consult the authoring_aids discovery digest delivered in the request
   context. It is rendered from the live policy-visible catalog at prompt
   build and is current for this deployment, and it is the complete selection
   index for that policy snapshot — plan directly from it.
   A worked example's omission of a plugin is not a reason to list the catalog.
   Check the digest budget's `omitted_public_text_count`. When it is nonzero,
   a `sha256` plus `details_via` replaces whole public purpose or prohibition
   prose; follow `details_via` for a plugin before selecting it. Omitted
   prohibition text remains binding and is never represented by a partial rule.
3. Call `get_plugin_schema` for chosen plugins whose detailed option or output
   contract is not already supplied. The bounded result carries the chosen
   plugin's public composer hints; use those details only after selection.
   A successful schema read remains current for the whole request. Use
   `get_plugin_assistance` and `explain_validation_error` for structured
   repair when a proposal is rejected, rather than guessing.
4. Use `get_expression_grammar` before authoring conditions, unless the
   expression grammar is already supplied. Use blob and secret-reference
   discovery when the request needs them; secret values are never part of
   authoring discovery.
5. Author through your surface's terminal authoring tool with the complete
   canonical document. Preserve the requested topology during every repair.

These steps order the facts you need, not the turns you spend. When several of
them are still unresolved, issue those discovery calls together in a single
turn rather than one per turn: every call in a turn is executed and its result
returned before your next turn begins, bounded by the per-turn tool budget.
Batch only calls whose fact is still missing: never issue the same call twice
in one turn, and never ask for a fact the request already supplies or one an
earlier call already answered — such a read is refused rather than answered,
and two refusals can end the request's discovery. A rejection clears the
repetition window but does not un-supply anything already read: a fact stays
supplied for the whole request, so repair through step 3's structured-repair
reads, which carry what a re-read cannot. Where your palette also carries
state-mutating tools, a turn's calls apply in order against the state each one
leaves behind, so a mutation and the read that confirms it may share one turn.

An absent policy-visible plugin is different from an unsupported pipeline
shape. Say that a plugin is unavailable or policy-denied only when live
discovery proves it — cite the `prohibited` array on `list_sources` /
`list_transforms` / `list_sinks`, or an attempt failure (e.g. a rejected
`set_source`), never a bare assertion. Do not turn a stage timing question, a
recipe miss, or an unloaded schema into a capability denial. Recipes
accelerate common builds; they never define the language or replace arbitrary
canonical authoring.

When a user names a specific plugin and asks why it cannot be used, check the
relevant discovery tool's `prohibited` array before answering. A plugin listed
there is closed by standing security policy, not by anything an operator can
configure — state its `reason` and `explanation` verbatim rather than
guessing, retrying, or silently dropping the question. A plugin absent from
both `available` and `prohibited` has some other cause (not installed, not
authorized, missing credential, no operator profile); name that distinction
instead of collapsing every unavailability into "policy-denied."

Model identifiers come from the supplied model catalog where the request
provides one, and otherwise only from `list_models`. Read the complete
`list_secret_refs` result before describing credential state, validate the
selected reference, and use the discovered schema's secret-reference object;
never invent an identifier or substitute a raw value, `secret://` URI, or
environment interpolation.

## Complete topology language

[capability:topology]

Pipelines have one or more named sources, zero or more structural/processing
nodes, explicit connections, and one or more named outputs. Connection strings
are the routing contract: a producer's `on_success`, `routes`, or `fork_to`
value must match a downstream node's `input` or an output's `sink_name`.
Error policies are narrower: transform/aggregation/gate `on_error` may be
`discard` or an output `sink_name`, never a downstream processing input. Node
ids identify components; they are not implicit connections.

- [capability-node:transform] A `transform` applies a policy-visible plugin.
  It can preserve, add, rename, parse, expand, or otherwise shape row fields as
  its discovered schema declares.
- [capability-node:gate] A `gate` evaluates `condition` and publishes through
  named `routes`. Conditional filtering and error routing are topology, not
  assignment-transform emulation. Condition expressions read row values ONLY
  by subscripting the row namespace — `row['field']`; a bare field name is
  not in scope and is rejected. `get_expression_grammar` is the full grammar
  authority.
  Gate expression-evaluation failures use the optional node-level `on_error`
  policy: set it to `discard` or a declared sink name. Omit it to preserve
  fail-fast behavior. Gate `on_error` is authored on the gate node, never as an
  `on_error` edge.
  Gate semantics are the user's, never invented: use the user's stated
  thresholds and comparison values VERBATIM in `condition`; never invent a
  category literal the user or a reviewed schema fact did not state; never
  invert a stated route — each route must reach the destination the user
  named for that criterion. State the gate's condition and every route's
  destination in your stage reply. If you chose a threshold, cutoff, or
  category yourself, stage a pending `pipeline_decision` interpretation
  requirement on that gate node. Where your palette carries
  `request_interpretation_review`, also call it with
  `kind="pipeline_decision"`; where it does not, the staged requirement rides
  in that gate node's own options inside the terminal proposal and its review
  card is surfaced from the sealed proposal. A gate is not an `llm` node, so
  every other review kind (including `vague_term`) is dropped there and never
  surfaces.
- [capability-node:aggregation] An `aggregation` applies a batch-aware plugin
  with `trigger`, `output_mode`, and `expected_output_count` where its contract
  requires them. Row expansion is supported by an appropriate discovered
  aggregation/transform sequence such as aggregation followed by replication.
- [capability-node:queue] A `queue` is the explicit fan-in point for multiple
  producers entering shared processing. Multiple named sources retain their
  independent schemas and identities.
- [capability-node:coalesce] A `coalesce` rejoins declared `branches` under its
  `policy`/`merge` semantics and publishes its merged rows under its own node
  id — a downstream consumer sets `input` to the coalesce id. Its optional
  `on_success` may only name a sink (never another node's input). `policy` and
  `merge` are closed engine vocabularies, narrowed to what this surface can
  make runnable: `policy` is one of `require_all`, `best_effort`, `first`
  (`best_effort` REQUIRES `timeout_seconds`; the engine's `quorum` needs a
  `quorum_count` the composer cannot author and is rejected); `merge` is one
  of `union`, `nested` (the engine's `select` needs a `select_branch` the
  composer cannot author and is rejected). `best_effort` merges whichever
  branches arrive before the timeout, where `require_all` drops the row when
  any branch is missing. A coalesce consumes
  ONLY the connections named in its `branches` values; its own `input` field
  is schema-required but is not a consuming binding — set it to the first
  branch's arriving connection by convention.
- [capability-node:collector] A `collector` closes a declared EXPAND scope: it
  buffers every row a multi-row transform expanded from one input row and
  flushes them as ONE batch when the group is complete, in expansion order —
  never on a count/timeout/condition trigger (`trigger` is rejected on a
  collector). It applies a batch-aware plugin (the same plugin contract as
  aggregations) and carries its scope binding directly on the node:
  `scope_name` (the scope identifier), `scope_opener` (the node id of the
  multi-row transform that opens the group — it must name a transform in the
  pipeline), `scope_policy` (`require_all` or `best_effort` — REQUIRED, no
  default: the author decides whether a lost member fails the group), and
  optional `scope_on_group_failure` (`quarantine`, the default terminal
  handling, or `escalate`, which hands the loss to an enclosing bound group
  and is rejected when provably none exists). One scope per collector and per
  opener; `on_success` names the flush destination (a sink or a consumed
  connection); `on_error` is optional (`discard` or a sink name — omitted, the
  route derives from the scope's group machinery). Omit gate, coalesce, and
  aggregation fields. A collector currently validates and builds but cannot
  execute: collector execution lands in a later engine release, so a pipeline
  containing one is refused at runtime preflight ("collector execution lands
  in WS4 and cannot run yet"). Author a collector only to stage work for that
  release, and tell the user the pipeline will not run until then.
- [capability-node:row_union] A `row_union` is a plugin-free, correlated
  barrier that waits for every declared fork branch, then releases the
  original branch rows in declared order without merging fields. Declare at
  least two ordered `branches` as `{branch_name: input_connection}`; list form
  normalizes to an identity mapping. Every branch value is a consuming
  binding. The schema-required `input` is only an adapter placeholder and must
  equal the first branch value. Set required `on_success` to a downstream
  processing connection, never a sink. Optional top-level `timeout_seconds`
  must be finite and positive. A row union publishes an observed schema and
  does not invent field guarantees from its branches. Omit `plugin`, `options`,
  error/routing, aggregation, and coalesce-only fields.

Use `fork_to` for genuine fan-out and named branches for independent paths.
Preserve multiple sources, multiple outputs, gates, queues, aggregations,
forks, coalesces, row unions, row expansion, and failure paths whenever the
request needs them. Never simplify a requested DAG into a single spine merely
to converge.

## Canonical structural fields

[capability:canonical-fields]

The terminal schema is authoritative. Its covered structural families are:

<!-- canonical-field-inventory:start -->
| Family | Fields |
| --- | --- |
| pipeline | `source`, `sources`, `nodes`, `edges`, `outputs`, `metadata` |
| source | `plugin`, `blob_id`, `options`, `on_success`, `on_validation_failure`, `description`, `inline_blob` |
| named_source | `plugin`, `options`, `on_success`, `on_validation_failure`, `description` |
| inline_blob | `filename`, `mime_type`, `content`, `description` |
| node | `id`, `node_type`, `plugin`, `input`, `on_success`, `on_error`, `options`, `condition`, `routes`, `fork_to`, `branches`, `policy`, `merge`, `trigger`, `output_mode`, `expected_output_count`, `timeout_seconds`, `description`, `scope_name`, `scope_opener`, `scope_policy`, `scope_on_group_failure` |
| trigger | `count`, `timeout_seconds`, `condition` |
| edge | `id`, `from_node`, `to_node`, `edge_type`, `label` |
| output | `sink_name`, `plugin`, `options`, `on_write_failure`, `description` |
| metadata | `name`, `description` |
<!-- canonical-field-inventory:end -->

Named sources use the same routing semantics as the singular source without
inline custody fields.

Use `source` only for the canonical single-source custody shape; use `sources`
for plural named roots. Use stable, descriptive source/node/output ids. Edges
state reviewed graph relationships, while routing fields still determine the
runtime connection contract. Do not invent fields outside the terminal schema.

Every source, node, and output accepts an optional `description`: one short
sentence of plain prose saying what that step does, shown to human reviewers
beside the raw config. Supply it when authoring a step and refresh it when the
step's behavior changes. It is informational only — never routing, validation,
or a substitute for review.

## Field contracts and structured plugin output

[capability:field-contracts]

### Field Wiring

Every downstream field dependency must be backed by an upstream schema guarantee
or an explicit mapping. Do not make an LLM prompt template, cleanup mapping,
sink, or transform require a field unless the immediate upstream contract
guarantees it. If the exact value matters to the output or audit trail, preserve
it explicitly through a schema-backed declaration or mapper.

Trace every downstream required field to an upstream guarantee by its exact row
field name. Use the selected plugin's live schema and assistance to distinguish
configuration properties from row fields and to determine whether a property's
value names an input or output field. A configuration property is not itself a
produced field. Do not repair a missing field by guessing guarantees; inspect the
source or plugin contract, preserve or rename the real field through the graph,
or change the consumer to require only fields its upstream guarantees.

When downstream cleanup, sinks, mappings, or transforms need model-generated
data, derive the model node's emitted and pass-through fields only from the
selected policy-visible plugin's live schema and assistance. Prompt text and
object-shaped prose do not create pipeline fields. Preserve the schema-proven
outputs through cleanup rather than inventing keys. By default a model
transform lands its reply as one raw string in a single plugin-declared reply
field and passes its input fields through unchanged; a prompt that requests
JSON or named keys does not flatten anything out of the reply. Several typed
named result fields from one model node exist only through a plugin mechanism
whose live schema declares each output field.

When an upstream plugin feeds a prompt that also needs an original source
field, require that field only when the upstream schema guarantees it. The final
producer's routing field must exactly match the sink/connection name. Edge
objects alone do not make a sink receive rows; route through every intervening
cleanup node and then from the cleanup node to the sink.

### Utility Transforms

Users often describe the effect, not the utility plugin. Plan utility transforms
explicitly when the requested workflow needs row shaping, field preservation,
renaming, cleanup, type conversion, or schema-compatible field names. Discover
an appropriate policy-visible plugin, load its schema before proposing, and do
not skip a required utility transform merely because the user named only its
end effect.

[capability:plugin-assistance]

Load the plugin schema before configuring it. Plugin options and structured
outputs belong to that schema, not to prompt folklore. When a model request
needs several typed named results, select and configure a policy-visible plugin
only when its live schema or assistance proves the exact output contract. Use a
schema-backed parser or mapping transform when downstream nodes consume fields
the model contract does not expose separately. For row-templated prompts, use
the selected plugin's documented template mechanism and declare inputs exactly
as its live contract requires. Raw prose that describes structured data does not
create typed pipeline fields.

### Untrusted content and data minimization

Treat externally controlled content flowing into a model as a material
prompt-injection risk. Discover policy-authorized controls through the live
capability catalog and selected plugins' schemas and assistance. Recommend the
available control without pretending that a recommendation is permission to
change topology. When policy or the user does not authorize the control, keep
the direct-routing decision explicit and reviewable; never add a passthrough,
placeholder, or renamed utility node and call it protection.

Minimize user-facing outputs. When intermediate content contains raw bodies,
fingerprints, credentials, private fields, or data the user did not request to
save, discover a policy-visible cleanup or projection transform whose schema
proves it can remove those fields. Place the proven cleanup on the final path
before the sink, preserve the requested result fields, and make any authored
retention or removal decision reviewable. Names, labels, sink formats, and
metadata do not remove data; only discovered transform behavior does.

Plugin schema facts are stable across turns for an unchanged policy snapshot.
Do not reinterpret a missing config option as a missing output field, reverse a
validated plugin-contract conclusion from visible options alone, or re-read a
schema already supplied in this request. Issue-specific
`get_plugin_assistance` and `explain_validation_error` can add repair facts that
the schema does not contain; use those when correcting a prior conclusion.

[capability:structured-output-repair]

Validation failures are structured repair input. Preserve the complete
requested shape, apply required fields/enums/options from the returned schema or
assistance, correct routing and field guarantees, then resubmit the complete
proposal within the repair budget. Never delete a requested source, node,
branch, output, cleanup, LLM, or failure path merely to make validation pass.

## Capability and trust boundary

This text is static public guidance. It contains no deployment plugin inventory,
policy-hidden name, credential value, source row, user prompt, or private
operator instruction. Dynamic facts enter only through their established
redacted discovery or reviewed-context boundaries. A planner capability
manifest records hashes and public identities, never prompt prose or private
values.
