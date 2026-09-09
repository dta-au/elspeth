import React from "react";

/**
 * Chat message bubble for the ELSPETH composer conversation. `role` selects
 * the user (right, green tint), assistant (left, white tint + 2px left accent),
 * or system (centred, italic, muted) treatment.
 */
export function ChatBubble({ role = "assistant", className = "", children, ...rest }) {
  const rowJustify =
    role === "user" ? "flex-end" : role === "system" ? "center" : "flex-start";

  const bubbleBase = {
    padding: "var(--space-md) var(--space-lg)",
    borderRadius: "var(--radius-lg)",
    lineHeight: "var(--line-height-snug)",
    fontSize: "var(--font-size-base)",
    color: "var(--color-text)",
    wordBreak: "break-word",
    maxWidth: "min(80%, 68ch)",
  };

  const byRole = {
    user: {
      background: "var(--color-bubble-user)",
      border: "1px solid var(--color-bubble-user-border)",
      whiteSpace: "pre-wrap",
    },
    assistant: {
      background: "var(--color-bubble-assistant)",
      border: "1px solid var(--color-bubble-assistant-border)",
      borderLeft: "2px solid var(--color-border-strong)",
    },
    system: {
      background: "var(--color-bubble-system)",
      color: "var(--color-text-muted)",
      fontStyle: "italic",
      fontSize: "var(--font-size-sm)",
      textAlign: "center",
      maxWidth: "100%",
      width: "100%",
      border: "none",
    },
  };

  return (
    <div
      className={className}
      style={{ display: "flex", justifyContent: rowJustify, padding: "var(--space-xs) var(--space-lg)" }}
      {...rest}
    >
      <div style={{ ...bubbleBase, ...byRole[role] }}>{children}</div>
    </div>
  );
}
