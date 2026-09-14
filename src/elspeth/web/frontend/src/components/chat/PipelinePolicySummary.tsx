import type { CompositionState } from "@/types";
import { llmBindingLabel } from "@/lib/llmBindingLabel";

/** Saved policy is visible even when the planner's reply omits it. */
export function PipelinePolicySummary({ state }: { state: CompositionState | null }) {
  if (state === null) return null;

  const models = [
    ...Object.entries(state.sources)
      .filter(([, source]) => source.plugin === "llm")
      .map(([id, source]) => `${id}: ${llmBindingLabel(source.options)}`),
    ...state.nodes
      .filter((node) => node.plugin === "llm")
      .map((node) => `${node.id}: ${llmBindingLabel(node.options)}`),
  ];
  const discardedSources = Object.entries(state.sources)
    .filter(([, source]) => source.on_validation_failure === "discard")
    .map(([id]) => id);
  const discardedNodes = state.nodes.filter((node) => node.on_error === "discard");
  const discardedOutputs = state.outputs.filter((output) => output.on_write_failure === "discard");
  const allBranchMerges = state.nodes.filter(
    (node) => node.node_type === "coalesce" && node.policy === "require_all",
  );

  if (models.length + discardedSources.length + discardedNodes.length + discardedOutputs.length + allBranchMerges.length === 0) {
    return null;
  }

  return (
    <section aria-label="Pipeline model and failure policy" className="chat-panel-policy-summary" tabIndex={0}>
      <strong>Saved model and failure policy</strong>
      <ul>
        {models.length > 0 && <li>LLM selection — {models.join("; ")}. Profiles use the operator-configured model.</li>}
        {discardedSources.length > 0 && <li>{discardedSources.join(", ")}: invalid source rows are discarded; their audit outcome remains recorded.</li>}
        {discardedNodes.length > 0 && <li>{discardedNodes.map((node) => node.id).join(", ")}: row errors discard the affected row from this path; its audit outcome remains recorded.</li>}
        {allBranchMerges.length > 0 && <li>{allBranchMerges.map((node) => node.id).join(", ")}: every branch is required. A missing branch produces a recorded failed merge and no combined row reaches downstream output.</li>}
        {discardedOutputs.length > 0 && <li>{discardedOutputs.map((output) => `${output.name} (${output.plugin})`).join(", ")}: failed writes are discarded, so affected rows can be absent from the output; the failure remains recorded.</li>}
      </ul>
    </section>
  );
}
