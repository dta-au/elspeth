import {
  pluginDisplayName,
  titleCaseLabel,
} from "@/components/catalog/pluginDisplayName";
import { PipelineGloss } from "@/components/chat/guided/PipelineGloss";
import { OptionRows } from "@/components/inspector/OptionRows";
import { DISCARD_CONNECTION } from "@/lib/graphTopology";
import { useSessionStore } from "@/stores/sessionStore";
import type { CompositionState } from "@/types/index";
import {
  buildConnectionIndex,
  routingPhrase,
  type ConnectionIndex,
} from "./specRouting";

interface SpecRow {
  id: string;
  kind: string;
  plugin: string | null;
  pluginKind: "source" | "transform" | "sink";
  routing: Record<string, unknown>;
  options: Record<string, unknown>;
  description: string | null;
}

interface SpecSectionProps {
  name: "Sources" | "Nodes" | "Outputs";
  rows: SpecRow[];
  state: CompositionState;
  index: ConnectionIndex;
}

function displayValue(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

// Humanised labels/values for the routing dl (elspeth-b9ebdf9011). The
// plugin `options` block routes through the shared OptionRows renderer
// instead — this covers only the wiring fields projected in *Rows() below.
const ROUTING_LABELS: Record<string, string> = {
  input: "Reads from",
  on_success: "Then",
  on_error: "On error",
  on_validation_failure: "Rows failing validation",
  on_write_failure: "If writing fails",
  fork_to: "Forks every row to",
  routes: "Routes",
  branches: "Merges branches",
  policy: "Merge policy",
  merge: "Merge",
  scope_name: "Scope",
  scope_opener: "Scope opened by",
  scope_policy: "Scope policy",
  output_mode: "Output mode",
  timeout_seconds: "Waits up to (seconds)",
};

function routingLabel(field: string): string {
  // A field absent from the map is still an author-visible <dt>; title-case
  // it rather than printing bare snake_case. ROUTING_LABELS is believed
  // exhaustive over the fields *Rows() projects today, so this is a guard
  // against a future field being added to a builder and not to the map —
  // exactly the drift the Wave 1 live check found on the <dd> side.
  return ROUTING_LABELS[field] ?? titleCaseLabel(field);
}

/** Fields whose value is an author-chosen NAME (not a connection, not an
 *  enum): rendered title-cased with the raw in `title`, same rule as ids. */
const AUTHOR_NAME_FIELDS: ReadonlySet<string> = new Set(["scope_name"]);

/**
 * One routing <dd>. The reader-register phrase comes from specRouting, which
 * resolves a connection name to the component on the other end (and carries
 * the elspeth-b9ebdf9011 branches-as-prose fix: a `branches`/`routes` map
 * renders as prose, never as a raw JSON string in a plain <dd>). A null
 * phrase means the value is not a connection or an enum, and the rules below
 * apply.
 */
function RoutingDd({
  state,
  index,
  field,
  value,
}: {
  state: CompositionState;
  index: ConnectionIndex;
  field: string;
  value: unknown;
}): JSX.Element {
  if (value === DISCARD_CONNECTION) return <dd>dropped (recorded in the audit trail)</dd>;
  const phrase = routingPhrase(state, index, field, value);
  if (phrase !== null) return <dd title={phrase.raw}>{phrase.text}</dd>;
  if (AUTHOR_NAME_FIELDS.has(field) && typeof value === "string") {
    return <dd title={value}>{titleCaseLabel(value)}</dd>;
  }
  if (Array.isArray(value)) return <dd>{value.map(String).join(", ")}</dd>;
  return <dd>{displayValue(value)}</dd>;
}

function SpecSection({ name, rows, state, index }: SpecSectionProps): JSX.Element {
  const singular = name.slice(0, -1);
  const headingId = `pipeline-spec-${name.toLowerCase()}-heading`;
  return (
    <section
      className="pipeline-spec-section"
      role="region"
      aria-labelledby={headingId}
    >
      <h3 id={headingId}>{name}</h3>
      {rows.length === 0 ? (
        <p>{`No ${name.toLowerCase()}.`}</p>
      ) : (
        <div className="pipeline-spec-cards">
          {rows.map((row) => {
            const routingEntries = Object.entries(row.routing).filter(
              ([, value]) => value !== null,
            );
            return (
              <article
                key={row.id}
                className="pipeline-spec-card"
                aria-label={`${singular} ${row.id}`}
              >
                <h4 title={row.id}>{titleCaseLabel(row.id)}</h4>
                {row.description !== null && row.description.trim() !== "" && (
                  <p className="pipeline-spec-step-description">
                    {row.description}
                  </p>
                )}
                <dl>
                  <div>
                    <dt>Kind</dt>
                    <dd title={row.kind}>{titleCaseLabel(row.kind)}</dd>
                  </div>
                  {row.plugin !== null && (
                    <div>
                      <dt>Plugin</dt>
                      {/* Human register on the label, the raw catalog id in
                          `title` for operators to copy (elspeth-ca456d9d8d):
                          same treatment as the catalog card. */}
                      <dd title={row.plugin}>{pluginDisplayName(row.plugin)}</dd>
                    </div>
                  )}
                  {routingEntries.map(([field, value]) => (
                    <div key={field}>
                      <dt>{routingLabel(field)}</dt>
                      <RoutingDd state={state} index={index} field={field} value={value} />
                    </div>
                  ))}
                </dl>
                <OptionRows
                  options={row.options}
                  ariaLabel={`${singular} ${row.id} settings`}
                  plugin={row.plugin === null ? null : { kind: row.pluginKind, name: row.plugin }}
                />
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function sourceRows(state: CompositionState): SpecRow[] {
  return Object.entries(state.sources)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([id, source]) => ({
      id,
      kind: "source",
      plugin: source.plugin,
      pluginKind: "source",
      routing: {
        on_success: source.on_success ?? null,
        on_validation_failure: source.on_validation_failure ?? null,
      },
      options: source.options,
      description: source.description ?? null,
    }));
}

function nodeRows(state: CompositionState): SpecRow[] {
  return state.nodes.map((node) => ({
    id: node.id,
    kind: node.node_type,
    plugin: node.plugin,
    pluginKind: "transform",
    // Project every field that describes how a node is WIRED, for every node
    // kind (elspeth-59684fb0c8). The routing block drops nulls, so a node
    // carrying none of these is unaffected and gains no empty rows.
    //
    // The rule this enforces: where `input` alone understates a node's
    // wiring, the card must carry what completes it, or it does not merely
    // omit the topology — it ASSERTS a narrower one than the state has.
    //   * fan-in (coalesce, row_union): `input` is ONLY the backend-compatible
    //     first-branch placeholder; `branches` is the authoritative map.
    //   * collector: `input` names the connection, but the scope BINDING
    //     (which expand group it closes, under which arrival policy) lives in
    //     scope_name/scope_opener/scope_policy — 66 of 66 collectors in the
    //     saved corpus populate all three, and the collector's prose gloss is
    //     a fixed string that names none of them.
    //   * barrier kinds: `timeout_seconds` bounds how long the wait holds.
    //
    // `scope_name` is NOT private here. The "authored scope_name stays
    // private" note in types/guided.ts governs the GUIDED PROPOSAL payload
    // sent to the planner, where server stable ids replace canonical names.
    // This view renders the session owner's own accepted CompositionState,
    // which serialises scope_name openly (composer/state.py:866,894).
    //
    // `condition` stays out. The "shows only non-null authoritative routing
    // fields" test asserts its absence — deliberately, since it is the one
    // NON-null field that test excludes. The reason is unrecorded, and it is
    // NOT redaction: the same predicate renders in the Graph tab's inspector
    // (GraphView.tsx) and is carried verbatim into this very tab's prose by
    // PipelineGloss. Left excluded rather than flipped on an inference —
    // tracked for adjudication, not settled here.
    routing: {
      input: node.input,
      branches: node.branches ?? null,
      policy: node.policy ?? null,
      merge: node.merge ?? null,
      scope_name: node.scope_name ?? null,
      scope_opener: node.scope_opener ?? null,
      scope_policy: node.scope_policy ?? null,
      output_mode: node.output_mode ?? null,
      timeout_seconds: node.timeout_seconds ?? null,
      on_success: node.on_success,
      on_error: node.on_error,
      routes: node.routes ?? null,
      fork_to: node.fork_to ?? null,
    },
    options: node.options,
    description: node.description ?? null,
  }));
}

function outputRows(state: CompositionState): SpecRow[] {
  return state.outputs.map((output) => ({
    id: output.name,
    kind: "output",
    plugin: output.plugin,
    pluginKind: "sink",
    routing: {
      on_write_failure: output.on_write_failure ?? null,
    },
    options: output.options,
    description: output.description ?? null,
  }));
}

export function PipelineSpecView(): JSX.Element {
  const compositionState = useSessionStore((state) => state.compositionState);

  if (compositionState === null) {
    return <p className="empty-state">No pipeline specification yet.</p>;
  }

  // One index for the whole view: every routing <dd> resolves its connection
  // through it, in the direction its field means.
  const index = buildConnectionIndex(compositionState);
  const name = compositionState.metadata.name ?? "Untitled pipeline";
  const description =
    compositionState.metadata.description ?? "No description provided.";
  return (
    <div className="pipeline-spec-view">
      <header className="pipeline-spec-metadata">
        <h2>{name}</h2>
        <p>{description}</p>
        <PipelineGloss compositionState={compositionState} />
      </header>
      <SpecSection
        name="Sources"
        rows={sourceRows(compositionState)}
        state={compositionState}
        index={index}
      />
      <SpecSection
        name="Nodes"
        rows={nodeRows(compositionState)}
        state={compositionState}
        index={index}
      />
      <SpecSection
        name="Outputs"
        rows={outputRows(compositionState)}
        state={compositionState}
        index={index}
      />
    </div>
  );
}
