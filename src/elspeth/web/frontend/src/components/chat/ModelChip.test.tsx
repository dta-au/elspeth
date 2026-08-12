import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { ModelChip } from "./ModelChip";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";

// The chip is a pure store reader: App's health poll is the app's single
// /api/system/status consumer and publishes composerModel into the session
// store (one derivation per surface — a chip-owned fetch raced sequenced
// test doubles and double-fetched in production, elspeth-8fa71e6d15).

describe("ModelChip", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
  });

  it("shows the composer model from the store with an accessible label", () => {
    useSessionStore.setState({
      composerModel: "anthropic/claude-sonnet-4.6",
    });

    render(<ModelChip />);

    expect(
      screen.getByLabelText("Composer model: anthropic/claude-sonnet-4.6"),
    ).toBeInTheDocument();
    expect(screen.getByText("anthropic/claude-sonnet-4.6")).toBeInTheDocument();
  });

  it("renders nothing while no model is known", () => {
    // Absence of chrome, never a fabricated model name. composerModel stays
    // null until a successful health poll reports a non-empty model.
    const { container } = render(<ModelChip />);
    expect(container).toBeEmptyDOMElement();
  });
});
