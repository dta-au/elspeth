import * as React from "react";

/**
 * ELSPETH typographic wordmark, rendered as live text (JetBrains Mono 700,
 * uppercase, 0.18em tracking). There is no graphical logo — always use this.
 *
 * @startingPoint section="Composer" subtitle="The ELSPETH brand wordmark" viewport="700x100"
 */
export interface WordMarkProps extends React.HTMLAttributes<HTMLElement> {
  /** Font size in px (number) or any CSS length (string). @default 13 */
  size?: number | string;
  /** Element to render. @default "span" */
  as?: keyof JSX.IntrinsicElements;
}

export function WordMark(props: WordMarkProps): React.ReactElement;
