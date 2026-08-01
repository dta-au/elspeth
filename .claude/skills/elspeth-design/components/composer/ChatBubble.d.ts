import * as React from "react";

/**
 * A single chat message bubble in the ELSPETH composer. User bubbles sit right
 * with a green tint; assistant bubbles sit left with a 2px left accent;
 * system notices are centred, italic, and muted.
 *
 * @startingPoint section="Composer" subtitle="User / assistant / system chat bubbles" viewport="700x220"
 */
export interface ChatBubbleProps
  extends React.HTMLAttributes<HTMLDivElement> {
  /** Message author. @default "assistant" */
  role?: "user" | "assistant" | "system";
}

export function ChatBubble(props: ChatBubbleProps): React.ReactElement;
