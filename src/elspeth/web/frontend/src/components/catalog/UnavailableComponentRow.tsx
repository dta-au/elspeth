// ============================================================================
// UnavailableComponentRow — the ONE row for a disabled/unavailable saved
// component, shared by ImportYamlModal's preflight list and CatalogDrawer's
// "Unavailable saved components" section so the two cannot drift
// (elspeth-aa39cffb16). The authored component id is the actionable name and
// stays; the plugin renders by display name with the raw id demoted to a
// title attribute (copy register). Callers supply `key` on this element.
// The authored component id is the actionable name and stays — in <code>,
// the identifier register, because the user matches it against their own
// YAML (elspeth-59631ec7f7 ruling).
// ============================================================================

import type { ReactNode } from "react";

import { pluginDisplayName } from "./pluginDisplayName";

export interface UnavailableComponentFindingLike {
  component_id: string;
  plugin_id: string;
  reason_code: string;
}

/** `finding.plugin_id` is the wire composite `kind:name` (e.g.
 *  "transform:legacy_llm"), not the bare plugin name `pluginDisplayName`
 *  expects — passing the composite straight through humanises the "kind:"
 *  prefix instead of dropping it, producing wrong copy like
 *  "Transform:legacy LLM". Split off the kind prefix first so the row (and
 *  every aria-label referencing it) shows the plugin's actual display name
 *  ("Legacy LLM"); the raw composite id stays available via `title`. */
export function unavailablePluginDisplayName(pluginId: string): string {
  const separatorIndex = pluginId.indexOf(":");
  const namePart = separatorIndex === -1 ? pluginId : pluginId.slice(separatorIndex + 1);
  return pluginDisplayName(namePart);
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
        <strong><code>{finding.component_id}</code></strong>{" "}
        <span title={finding.plugin_id}>{unavailablePluginDisplayName(finding.plugin_id)}</span>{" "}
        — {reasonLabel}
      </div>
      <div className="import-yaml-actions">{actions}</div>
    </li>
  );
}
