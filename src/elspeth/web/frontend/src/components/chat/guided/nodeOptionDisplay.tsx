// ============================================================================
// nodeOptionDisplay.tsx — the ONE rendering of a node's projected option
// summary (`node_options_summary`), shared by the proposal card and the
// wire-stage review (I-2, design review 2026-09-02).
//
// The backend owns the key vocabulary, the rendering to text, and the
// bounds (`_NODE_OPTION_SUMMARY_ALLOWLIST` in guided/protocol.py); this
// module only labels and lays out. A knob pair ("Mapping: a → b") stays the
// plain labelled line it always was. The llm node's prompts are different in
// kind: they are the instruction the user is approving, so they render as
// pre-wrapped prose with the first lines visible and an explicit expand
// control, and — when the caller can route the change — ONE Edit that
// pre-targets the node in the existing revise / correction flow. The
// planner stays the author of the change (composer invariant 1): there is
// no local prompt editor here, by design.
// ============================================================================

import { useState } from "react";

import { Button } from "@/components/ui";
import type { NodeOptionSummary } from "@/types/guided";
import { humanToken } from "./behaviorSummary";

/** Option keys whose value is prose the user approves, not a knob. Mirrors
 *  the backend's `_NODE_OPTION_SUMMARY_PROMPT_KEYS`. */
const PROMPT_OPTION_KEYS: ReadonlySet<string> = new Set(["prompt_template", "system_prompt"]);

/** Labels in the approval card's vocabulary (OptionRows uses the same words
 *  for the llm keys), so the pre-commit card and the post-commit card name
 *  one thing one way. Unlisted keys read as their humanised token. */
const OPTION_LABELS: Readonly<Record<string, string>> = {
  prompt_template: "Prompt",
  system_prompt: "System prompt",
  model: "Model",
};

/** A prompt longer than this many lines OR characters starts collapsed. */
export const PROMPT_COLLAPSE_LINES = 6;
export const PROMPT_COLLAPSE_CHARS = 480;

export function isPromptOption(entry: NodeOptionSummary): boolean {
  return PROMPT_OPTION_KEYS.has(entry.key);
}

export function nodeOptionLabel(key: string): string {
  const listed = OPTION_LABELS[key];
  if (listed !== undefined) return listed;
  const label = humanToken(key);
  return `${label.charAt(0).toUpperCase()}${label.slice(1)}`;
}

/** "Mapping: a → b" — the server-owned option key as a sentence-case label
 *  beside the value the backend already rendered (R2-F3). */
export function nodeOptionText(entry: NodeOptionSummary): string {
  return `${nodeOptionLabel(entry.key)}: ${entry.value}`;
}

export function promptNeedsCollapse(text: string): boolean {
  return text.length > PROMPT_COLLAPSE_CHARS || text.split("\n").length > PROMPT_COLLAPSE_LINES;
}

/** The first lines of a prompt: at most PROMPT_COLLAPSE_LINES lines and
 *  PROMPT_COLLAPSE_CHARS characters, so a single very long line still
 *  collapses to a bounded preview. */
export function collapsedPromptText(text: string): string {
  return text.split("\n").slice(0, PROMPT_COLLAPSE_LINES).join("\n").slice(0, PROMPT_COLLAPSE_CHARS);
}

interface PromptBlockProps {
  entry: NodeOptionSummary;
  nodeLabel: string;
}

function PromptBlock({ entry, nodeLabel }: PromptBlockProps): JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const collapsible = promptNeedsCollapse(entry.value);
  const shown = collapsible && !expanded ? collapsedPromptText(entry.value) : entry.value;
  const label = nodeOptionLabel(entry.key);
  return (
    <div className="guided-node-prompt">
      {/* Label and text share one <p> so "Prompt: …" reads as a single
          sentence to assistive tech, exactly like the knob lines. The text is
          pre-wrapped: a prompt's line structure is part of what is being
          approved. */}
      <p className="guided-node-prompt__text">
        <span className="guided-node-prompt__label">{label}: </span>
        {shown}
        {collapsible && !expanded ? <span aria-hidden="true">…</span> : null}
      </p>
      {collapsible ? (
        <Button
          compact
          className="guided-node-prompt__toggle"
          aria-expanded={expanded}
          aria-label={
            expanded
              ? `Show less of the ${label.toLowerCase()} for ${nodeLabel}`
              : `Show full ${label.toLowerCase()} for ${nodeLabel}`
          }
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? "Show less" : `Show full ${label.toLowerCase()}`}
        </Button>
      ) : null}
    </div>
  );
}

export interface NodeOptionsSummaryProps {
  entries: NodeOptionSummary[];
  /** The node's display label — names the prompt controls for assistive tech. */
  nodeLabel: string;
  /** Route "change this prompt" into the caller's node-scoped revise /
   *  correction flow, pre-targeting this node. Omitted where the caller has
   *  no such flow to open; the summary then renders without an Edit. */
  onEdit?: () => void;
  /** Present but momentarily unavailable (the caller's revise controls are
   *  locked, e.g. mid-submit): the Edit stays visible and disabled, like the
   *  Revise buttons beside it, rather than vanishing. */
  editDisabled?: boolean;
}

export function NodeOptionsSummary({
  entries,
  nodeLabel,
  onEdit,
  editDisabled = false,
}: NodeOptionsSummaryProps): JSX.Element | null {
  if (entries.length === 0) return null;
  const hasPrompt = entries.some(isPromptOption);
  return (
    <div className="guided-node-options">
      {entries.map((entry) =>
        isPromptOption(entry) ? (
          <PromptBlock key={entry.key} entry={entry} nodeLabel={nodeLabel} />
        ) : (
          <p key={entry.key}>{nodeOptionText(entry)}</p>
        ),
      )}
      {hasPrompt && onEdit !== undefined ? (
        <p className="guided-node-options__actions">
          <Button
            compact
            className="guided-node-options__edit"
            aria-label={`Edit prompt for ${nodeLabel}`}
            disabled={editDisabled}
            onClick={onEdit}
          >
            Edit prompt
          </Button>
        </p>
      ) : null}
    </div>
  );
}
