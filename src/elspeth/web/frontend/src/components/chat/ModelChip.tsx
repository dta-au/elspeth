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

import { useSessionStore } from "@/stores/sessionStore";

export function ModelChip() {
  const model = useSessionStore((s) => s.composerModel);

  if (model === null) return null;

  return (
    <span className="chat-model-chip" aria-label={`Composer model: ${model}`}>
      <span className="chat-model-chip-label" aria-hidden="true">
        Composer:
      </span>{" "}
      {model}
    </span>
  );
}
