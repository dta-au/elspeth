import * as React from "react";

/**
 * Component-type badge for the six ELSPETH pipeline primitives. Each type has
 * a fixed colour: source (aqua-green), transform (amber), gate (purple),
 * sink (orange-red), aggregation (cyan), coalesce (cyan-teal).
 *
 * @startingPoint section="Core" subtitle="The six pipeline primitive badges" viewport="700x120"
 */
export interface TypeBadgeProps
  extends React.HTMLAttributes<HTMLSpanElement> {
  /** Which pipeline primitive. @default "source" */
  type?: "source" | "transform" | "gate" | "sink" | "aggregation" | "coalesce";
  /** Override the label (defaults to the type name). */
  children?: React.ReactNode;
}

export function TypeBadge(props: TypeBadgeProps): React.ReactElement;
