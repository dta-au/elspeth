import * as React from "react";

export interface AuditCharacteristic {
  label: string;
  /** Chip tone. @default "informational" */
  tone?: "positive" | "attention" | "informational";
}

/**
 * A plugin entry in the ELSPETH catalog drawer: name, component-type badge,
 * clamped description, and a strip of audit-characteristic chips. Composes
 * TypeBadge.
 *
 * @startingPoint section="Composer" subtitle="Plugin catalog card with audit chips" viewport="700x200"
 */
export interface PluginCardProps
  extends React.HTMLAttributes<HTMLDivElement> {
  /** Plugin display name, e.g. "Azure Blob". */
  name: React.ReactNode;
  /** Pipeline component type (colours the badge). @default "source" */
  type?: "source" | "transform" | "gate" | "sink" | "aggregation" | "coalesce";
  /** Optional plugin id shown in the badge instead of the type word. */
  kind?: string;
  description?: React.ReactNode;
  /** Audit-characteristic chips. */
  audit?: AuditCharacteristic[];
  /** When set, renders a "Try in composer" action. */
  onTry?: () => void;
}

export function PluginCard(props: PluginCardProps): React.ReactElement;
