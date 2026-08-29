// ============================================================================
// UnavailableComponentRow — the ONE row for a disabled/unavailable saved
// component, shared by ImportYamlModal's preflight list and CatalogDrawer's
// "Unavailable saved components" section so the two cannot drift
// (elspeth-aa39cffb16). The authored component id is the actionable name and
// stays; the plugin renders by display name with the raw id demoted to a
// title attribute (copy register). Callers supply `key` on this element.
// ============================================================================

import type { ReactNode } from "react";

import { pluginDisplayName } from "./pluginDisplayName";

export interface UnavailableComponentFindingLike {
  component_id: string;
  plugin_id: string;
  reason_code: string;
}

export function UnavailableComponentRow({
  finding,
  reasonLabel,
  actions,
}: {
  finding: UnavailableComponentFindingLike;
  reasonLabel: string;
  actions: ReactNode;
}): JSX.Element {
  return (
    <li className="validation-banner-error-item">
      <div>
        <strong>{finding.component_id}</strong>{" "}
        <span title={finding.plugin_id}>{pluginDisplayName(finding.plugin_id)}</span>{" "}
        — {reasonLabel}
      </div>
      <div className="import-yaml-actions">{actions}</div>
    </li>
  );
}
