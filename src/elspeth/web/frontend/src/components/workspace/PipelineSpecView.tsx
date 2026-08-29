import { PipelineGloss } from "@/components/chat/guided/PipelineGloss";
import { OptionRows } from "@/components/inspector/OptionRows";
import { useSessionStore } from "@/stores/sessionStore";
import type { CompositionState } from "@/types/index";

interface SpecRow {
  id: string;
  kind: string;
  plugin: string | null;
  routing: Record<string, unknown>;
  options: Record<string, unknown>;
  description: string | null;
}

interface SpecSectionProps {
  name: "Sources" | "Nodes" | "Outputs";
  rows: SpecRow[];
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
  return ROUTING_LABELS[field] ?? field;
}

function routingValue(field: string, value: unknown): string {
  if (value === "discard") return "dropped (recorded in the audit trail)";
  if (Array.isArray(value)) return value.map(String).join(", ");
  if (field === "routes" && typeof value === "object" && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.every(([, target]) => target === "fork")) return "every row continues to all branches";
    return entries.map(([when, target]) => `${when} → ${String(target)}`).join("; ");
  }
  return displayValue(value);
}

function SpecSection({ name, rows }: SpecSectionProps): JSX.Element {
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
                <h4>{row.id}</h4>
                {row.description !== null && row.description.trim() !== "" && (
                  <p className="pipeline-spec-step-description">
                    {row.description}
                  </p>
                )}
                <dl>
                  <div>
                    <dt>Kind</dt>
                    <dd>{row.kind}</dd>
                  </div>
                  {row.plugin !== null && (
                    <div>
                      <dt>Plugin</dt>
                      <dd>{row.plugin}</dd>
                    </div>
                  )}
                  {routingEntries.map(([field, value]) => (
                    <div key={field}>
                      <dt>{routingLabel(field)}</dt>
                      <dd>{routingValue(field, value)}</dd>
                    </div>
                  ))}
                </dl>
                <OptionRows options={row.options} ariaLabel={`${singular} ${row.id} settings`} />
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
      <SpecSection name="Sources" rows={sourceRows(compositionState)} />
      <SpecSection name="Nodes" rows={nodeRows(compositionState)} />
      <SpecSection name="Outputs" rows={outputRows(compositionState)} />
    </div>
  );
}
