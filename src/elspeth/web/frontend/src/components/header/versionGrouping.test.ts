import { describe, expect, it } from "vitest";

import type { CompositionStateVersion } from "@/types/index";
import { buildVersionRows } from "./versionGrouping";
import type { VersionLabelKind } from "./versionLabels";

function v(version: number): CompositionStateVersion {
  return { id: `id-${version}`, version, created_at: "2026-08-29T10:00:00Z", node_count: 11 };
}
const versions = [19, 18, 17, 16, 15, 14, 13, 12, 11].map(v);
// v14 is the applied row the run splits around; the return annotation (not
// `as const`, which TypeScript rejects on a conditional) pins the literals.
const kindFor = (row: CompositionStateVersion): VersionLabelKind =>
  row.version === 14 ? "applied" : "edited";

describe("buildVersionRows", () => {
  it("groups consecutive edited runs, keeps current and applied rows standalone", () => {
    const rows = buildVersionRows(versions, kindFor, 19, false, new Set());
    expect(rows.map((row) => (row.kind === "group" ? row.id : `v${row.version.version}`))).toEqual([
      "v19", "v15-v18", "v14", "v11-v13",
    ]);
  });

  it("marks a group expanded without changing the row list (members render nested)", () => {
    const rows = buildVersionRows(versions, kindFor, 19, false, new Set(["v15-v18"]));
    const group = rows[1];
    expect(group.kind).toBe("group");
    expect(group.kind === "group" && group.expanded).toBe(true);
    expect(group.kind === "group" && group.versions.map((m) => m.version)).toEqual([18, 17, 16, 15]);
  });

  it("never groups a single edited row, and show_advanced renders everything flat", () => {
    expect(buildVersionRows([v(19), v(18)], () => "edited", 19, false, new Set()).every((r) => r.kind === "version")).toBe(true);
    const flat = buildVersionRows(versions, kindFor, 19, true, new Set());
    expect(flat.every((row) => row.kind === "version")).toBe(true);
    expect(flat).toHaveLength(9);
  });

  it("keeps every version reachable through its group (revert safety, epic rule 1)", () => {
    const rows = buildVersionRows(versions, kindFor, 19, false, new Set());
    const reachable = rows.flatMap((row) => (row.kind === "group" ? row.versions : [row.version]));
    expect(reachable.map((m) => m.version).sort((a, b) => a - b)).toEqual([11, 12, 13, 14, 15, 16, 17, 18, 19]);
  });
});
