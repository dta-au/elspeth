import * as React from "react";

/**
 * Text input on the ELSPETH elevated surface with a strong border and visible
 * focus ring. Optional label + hint. `mono` for forensic-register values
 * (secret names, paths, hashes).
 *
 * @startingPoint section="Forms" subtitle="Labelled text input" viewport="700x140"
 */
export interface InputProps
  extends React.InputHTMLAttributes<HTMLInputElement> {
  /** Field label rendered above the control. */
  label?: React.ReactNode;
  /** Helper text rendered below the control. */
  hint?: React.ReactNode;
  /** Render the value in JetBrains Mono. @default false */
  mono?: boolean;
}

export function Input(props: InputProps): React.ReactElement;
