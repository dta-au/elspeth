import { CodeBlock } from "@/components/chat/CodeBlock";
import { PipelineGloss } from "@/components/chat/guided/PipelineGloss";
import { useSessionStore } from "@/stores/sessionStore";
import type { CompositionState } from "@/types/index";

interface SpecRow {
  id: string;
  kind: string;
  plugin: string | null;
  routing: Record<string, unknown>;
  options: Record<string, unknown>;
}

interface SpecSectionProps {
  name: "Sources" | "Nodes" | "Outputs";
  rows: SpecRow[];
}

function displayValue(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
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
                <dl>
                  <div>
                    <dt>id</dt>
                    <dd>{row.id}</dd>
                  </div>
                  <div>
                    <dt>kind</dt>
                    <dd>{row.kind}</dd>
                  </div>
                  <div>
                    <dt>plugin</dt>
                    <dd>{row.plugin ?? "None"}</dd>
                  </div>
                  {routingEntries.map(([field, value]) => (
                    <div key={field}>
                      <dt>{field}</dt>
                      <dd>{displayValue(value)}</dd>
                    </div>
                  ))}
                </dl>
                <div
                  className="pipeline-spec-options"
                  role="region"
                  aria-label={`${singular} ${row.id} options`}
                  tabIndex={0}
                >
                  <CodeBlock
                    code={JSON.stringify(row.options, null, 2)}
                    language="json"
                    prettyJson
                    showCopy={false}
                  />
                </div>
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
    }));
}

function nodeRows(state: CompositionState): SpecRow[] {
  return state.nodes.map((node) => ({
    id: node.id,
    kind: node.node_type,
    plugin: node.plugin,
    routing: {
      input: node.input,
      on_success: node.on_success,
      on_error: node.on_error,
      routes: node.routes ?? null,
      fork_to: node.fork_to ?? null,
    },
    options: node.options,
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
  }));
}

export function PipelineSpecView(): JSX.Element {
  const compositionState = useSessionStore((state) => state.compositionState);

  if (compositionState === null) {
    return <p className="artifact-empty">No pipeline specification yet.</p>;
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
