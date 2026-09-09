// Persistent composer-authority chip (elspeth-f5e6723133).
//
// The session's trust_mode has been loaded into the store since the
// preferences work landed, but no surface ever showed it: users could not
// tell whether the composer applies mutations immediately (auto_commit, the
// default) or stages them as proposals (explicit_approve). The chip names
// that authority in the chat header, beside the model identity chip.

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { AuthorityChip } from "./AuthorityChip";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";
import type { ComposerPreferences } from "@/types/api";

function preferences(trustMode: "auto_commit" | "explicit_approve"): ComposerPreferences {
  return {
    session_id: "session-1",
    trust_mode: trustMode,
    density_default: "high",
    interpretation_review_disabled: false,
    updated_at: "2026-08-06T00:00:00Z",
  };
}

describe("AuthorityChip", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
  });

  it("names auto-apply authority when the session is auto_commit", () => {
    useSessionStore.setState({ composerPreferences: preferences("auto_commit") });
    render(<AuthorityChip />);

    const chip = screen.getByText("Auto-apply on");
    expect(chip).toBeInTheDocument();
    // The plain-language consequence rides on the title so the label can
    // stay chip-sized while the full meaning stays discoverable.
    expect(
      screen.getByLabelText(/Composer authority: Auto-apply on/),
    ).toHaveAttribute("title", expect.stringContaining("immediately"));
  });

  it("names approval-gated authority when the session is explicit_approve", () => {
    useSessionStore.setState({ composerPreferences: preferences("explicit_approve") });
    render(<AuthorityChip />);

    expect(screen.getByText("Approval required")).toBeInTheDocument();
    expect(
      screen.getByLabelText(/Composer authority: Approval required/),
    ).toHaveAttribute("title", expect.stringContaining("proposals"));
  });

  it("renders nothing while preferences are unknown — never a fabricated authority", () => {
    const { container } = render(<AuthorityChip />);
    expect(container).toBeEmptyDOMElement();
  });
});
