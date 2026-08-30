// src/components/chat/ModelChip.tsx
//
// Composer-model identity chip in the AppHeader identity cluster
// (elspeth-e9f7678de8; relocated from the chat-panel headers by
// elspeth-8fa71e6d15). ELSPETH is an auditability product whose runs record
// model identity — the authoring surface should show which model is doing
// the composing, not leave it discoverable only through an LLM-side
// list_models lookup.
//
// The model is deployment configuration (ELSPETH_WEB__COMPOSER_MODEL),
// published into the session store by App's health poll — the app's single
// /api/system/status consumer. The chip deliberately does NOT fetch: a
// second consumer of that endpoint double-fetched in production and raced
// sequenced test doubles. When no model is known the chip renders nothing —
// absence of chrome, never a fabricated model name.

import { modelDisplayName } from "@/components/chat/modelDisplayName";
import { useSessionStore } from "@/stores/sessionStore";

export function ModelChip() {
  const model = useSessionStore((s) => s.composerModel);

  if (model === null) return null;

  // No ARIA at all: the chip reads "Composer: Claude Sonnet 4.6" as ordinary
  // text, to every audience. The previous `aria-label` sat on a role-less
  // <span> (role=generic), where ARIA 1.2 prohibits the attribute and AT
  // ignores it — the same defect PluginCard.tsx fixed under
  // elspeth-37293a3b7c — and it was aggravated by `aria-hidden` on the only
  // context word, so conforming AT announced a bare model name. Un-hiding
  // "Composer:" is the fix; a `role` on the outer span is NOT (role="img"
  // would make the children presentational, so the display name would stop
  // being separately readable). The raw id stays in `title`.
  return (
    <span className="chat-model-chip" title={model}>
      <span className="chat-model-chip-label">Composer:</span>{" "}
      {modelDisplayName(model)}
    </span>
  );
}
