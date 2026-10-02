import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { ModelChip } from "./ModelChip";
import { useSessionStore } from "@/stores/sessionStore";
import { expectNoIdentifiersInDefaultDom } from "@/test/defaultDomPins";
import { resetStore } from "@/test/store-helpers";

// The chip is a pure store reader: App's health poll is the app's single
// /api/system/status consumer and publishes composerModel into the session
// store (one derivation per surface — a chip-owned fetch raced sequenced
// test doubles and double-fetched in production, elspeth-8fa71e6d15).

describe("ModelChip", () => {
  beforeEach(() => {
    resetStore(useSessionStore);
  });

  it("shows the composer model's display name with the raw id in title", () => {
    useSessionStore.setState({
      composerModel: "anthropic/claude-sonnet-4.6",
    });

    render(<ModelChip />);

    // The visible label says "Composer:", not "Model:" — the chip names the
    // composing model, which must stay distinguishable from LLM models
    // configured inside the pipeline being authored. It is no longer
    // aria-hidden: the chip carries no ARIA, so this word is the only thing
    // saying what the model name names (elspeth-37293a3b7c).
    expect(screen.getByText("Composer:")).toBeInTheDocument();
    expect(screen.getByText("Claude Sonnet 4.6")).toBeInTheDocument();
    expect(
      screen.getByTitle("anthropic/claude-sonnet-4.6"),
    ).toBeInTheDocument();
  });

  it("keeps the raw model id out of visible text", () => {
    useSessionStore.setState({
      composerModel: "openrouter/anthropic/claude-sonnet-5",
    });

    const { container } = render(<ModelChip />);

    expectNoIdentifiersInDefaultDom(container);
    expect(screen.getByText("Claude Sonnet 5")).toBeInTheDocument();
    expect(container).not.toHaveTextContent("openrouter");
  });

  it("renders nothing while no model is known", () => {
    // Absence of chrome, never a fabricated model name. composerModel stays
    // null until a successful health poll reports a non-empty model.
    const { container } = render(<ModelChip />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the advisor model beside the composer model, labelled and with its raw id in title", () => {
    useSessionStore.setState({
      composerModel: "anthropic/claude-sonnet-4.6",
      composerAdvisorModel: "anthropic/claude-opus-4-7",
    });

    const { container } = render(<ModelChip />);

    // "Advisor:" names the model that gates completion, distinct from the
    // composing model; both stay ordinary text with raw ids in `title`.
    expect(screen.getByText("Composer:")).toBeInTheDocument();
    expect(screen.getByText("Advisor:")).toBeInTheDocument();
    // Hyphen-separated numeric version parts render dotted ("4-7" -> "4.7"),
    // not as disconnected words ("4 7") -- modelDisplayName.test.ts pins the
    // mechanism.
    expect(screen.getByText("Claude Opus 4.7")).toBeInTheDocument();
    expect(screen.getByTitle("anthropic/claude-opus-4-7")).toBeInTheDocument();
    expect(screen.getByTitle("anthropic/claude-sonnet-4.6")).toBeInTheDocument();
    expectNoIdentifiersInDefaultDom(container);
  });
});
