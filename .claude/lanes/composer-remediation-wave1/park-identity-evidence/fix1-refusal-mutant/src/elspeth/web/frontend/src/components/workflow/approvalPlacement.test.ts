import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Where the approval controls live is an operator ruling, not a layout
 * preference: a blocking state and the control that clears it sit at the top
 * level (the chat / action bar), never behind the Pipeline → Checks sub-tab.
 * A pending approval withholds execution, so it is a blocking state.
 *
 * This pins the placement itself. The behavioural half — that the Checks panel
 * renders no approval control — lives in AuditReadinessPanel.test.tsx; this
 * file pins the other direction, that the chat panel is what renders it, which
 * no rendering test covers because ChatPanel's harness does not reach here.
 *
 * It reads source rather than rendering, so it pins the WIRING and says so:
 * it cannot tell you the row is visible, only that the chat panel is the thing
 * that mounts it and the sub-tab is not.
 */

const SRC = join(__dirname, "..", "..");
const read = (relative: string): string => readFileSync(join(SRC, relative), "utf8");

const COMPONENT = "ApprovalReadinessRow";

describe("approval control placement", () => {
  it("is mounted by the chat panel, at the top level", () => {
    const chatPanel = read("components/chat/ChatPanel.tsx");
    expect(chatPanel).toContain(`import { ${COMPONENT} }`);
    expect(chatPanel).toMatch(new RegExp(`<${COMPONENT}\\b`));
  });

  it("is rendered beside the decision panel, not somewhere else in the chat", () => {
    const chatPanel = read("components/chat/ChatPanel.tsx");
    // Every decision-panel mount site carries the approval row with it, so the
    // two cannot drift apart across the panel's three modes.
    const panelSites = chatPanel.match(/\{decisionPanel\}/g) ?? [];
    const pairedSites = chatPanel.match(/\{decisionPanel\}\s*\n\s*\{approvalReadiness\}/g) ?? [];
    expect(panelSites.length).toBeGreaterThan(0);
    expect(pairedSites).toHaveLength(panelSites.length);
  });

  it("is not mounted by the Checks sub-tab", () => {
    expect(read("components/audit/AuditReadinessPanel.tsx")).not.toContain(COMPONENT);
  });

  it("is mounted in exactly one place, so the two cannot both be live", () => {
    const mounts = ["components/chat/ChatPanel.tsx", "components/audit/AuditReadinessPanel.tsx"]
      .filter((file) => read(file).includes(`<${COMPONENT}`));
    expect(mounts).toEqual(["components/chat/ChatPanel.tsx"]);
  });
});
