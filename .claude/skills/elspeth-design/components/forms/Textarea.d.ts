import * as React from "react";

/**
 * Multiline text input (vertical resize) matching the ELSPETH input styling.
 * Used by the chat composer, prompt editing, and amend forms.
 *
 * @startingPoint section="Forms" subtitle="Multiline textarea" viewport="700x160"
 */
export interface TextareaProps
  extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: React.ReactNode;
  hint?: React.ReactNode;
}

export function Textarea(props: TextareaProps): React.ReactElement;
