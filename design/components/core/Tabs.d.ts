import * as React from "react";

export interface TabItem {
  id: string;
  label: React.ReactNode;
  /** Optional count pill (used by the catalog tab strip). */
  count?: number;
}

/**
 * Underline tab strip — the bottom-border-accent tabs used across ELSPETH
 * (catalog, settings). Controlled.
 *
 * @startingPoint section="Core" subtitle="Underline tab strip with optional counts" viewport="700x120"
 */
export interface TabsProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "onChange"> {
  tabs: TabItem[];
  /** id of the active tab. */
  value: string;
  /** Called with the id of the newly selected tab. */
  onChange?: (id: string) => void;
}

export function Tabs(props: TabsProps): React.ReactElement;
