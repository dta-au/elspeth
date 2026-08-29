// ============================================================================
// versionGrouping — collapses runs of indistinguishable edited versions in
// the header history tree (elspeth-c8a402a9a4). Grouping is presentation
// only: every member stays in its group's `versions`, so every version a
// standard user could revert to remains reachable (epic rule 1 — Revert is an
// ACTION; grouping must never remove a revert target). Applied versions,
// reverts, the v1 seed, snapshot-only rows, and the current version always
// stand alone: their labels carry information a reader acts on. Grouping keys
// on versionLabelKind, never on the visible copy.
// ============================================================================

import type { CompositionStateVersion } from "@/types/index";
import type { VersionLabelKind } from "./versionLabels";

export interface VersionRow {
  kind: "version";
  version: CompositionStateVersion;
}
export interface GroupRow {
  kind: "group";
  id: string;
  versions: CompositionStateVersion[];
  expanded: boolean;
}
export type VersionListRow = VersionRow | GroupRow;

export function groupId(members: CompositionStateVersion[]): string {
  const numbers = members.map((member) => member.version);
  return `v${Math.min(...numbers)}-v${Math.max(...numbers)}`;
}

export function buildVersionRows(
  displayVersions: CompositionStateVersion[],
  kindFor: (version: CompositionStateVersion) => VersionLabelKind | "snapshot",
  currentVersion: number | null,
  showAdvanced: boolean,
  expandedGroupIds: ReadonlySet<string>,
): VersionListRow[] {
  if (showAdvanced) {
    return displayVersions.map((version) => ({ kind: "version", version }));
  }
  const rows: VersionListRow[] = [];
  let run: CompositionStateVersion[] = [];
  const flushRun = (): void => {
    if (run.length >= 2) {
      const id = groupId(run);
      rows.push({
        kind: "group",
        id,
        versions: run,
        expanded: expandedGroupIds.has(id),
      });
    } else {
      for (const version of run) {
        rows.push({ kind: "version", version });
      }
    }
    run = [];
  };
  for (const version of displayVersions) {
    const groupable =
      version.version !== currentVersion && kindFor(version) === "edited";
    if (groupable) {
      run.push(version);
    } else {
      flushRun();
      rows.push({ kind: "version", version });
    }
  }
  flushRun();
  return rows;
}
