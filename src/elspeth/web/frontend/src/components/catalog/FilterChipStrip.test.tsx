import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FilterChipStrip, type CatalogFilters } from "./FilterChipStrip";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { resetStore } from "@/test/store-helpers";

const ALL_OFF: CatalogFilters = {
  capabilityTags: new Set(),
  auditCharacteristics: new Set(),
};

beforeEach(() => resetStore(usePreferencesStore));

describe("FilterChipStrip", () => {
  it("exposes the strip as a group named 'Catalog filters' (WCAG 1.3.1)", () => {
    // aria-label on a role-less div is not exposed to AT; the strip must
    // carry role="group" for the label to associate (elspeth-37293a3b7c).
    render(
      <FilterChipStrip
        availableCapabilityTags={["csv"]}
        availableAuditCharacteristics={[]}
        filters={ALL_OFF}
        onChange={() => {}}
      />,
    );
    expect(
      screen.getByRole("group", { name: "Catalog filters" }),
    ).toBeInTheDocument();
  });

  it("renders one chip per capability tag", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    render(
      <FilterChipStrip
        availableCapabilityTags={["csv", "file", "http"]}
        availableAuditCharacteristics={[]}
        filters={ALL_OFF}
        onChange={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: /csv/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^http/i })).toBeInTheDocument();
  });

  it("emits an updated filter set when a chip is toggled", async () => {
    usePreferencesStore.setState({ showAdvanced: true });
    const onChange = vi.fn();
    render(
      <FilterChipStrip
        availableCapabilityTags={["csv"]}
        availableAuditCharacteristics={[]}
        filters={ALL_OFF}
        onChange={onChange}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /csv/i }));
    expect(onChange).toHaveBeenCalled();
    const updated: CatalogFilters = onChange.mock.calls[0][0];
    expect(updated.capabilityTags.has("csv")).toBe(true);
  });

  it("toggling an active chip removes it", async () => {
    usePreferencesStore.setState({ showAdvanced: true });
    const onChange = vi.fn();
    render(
      <FilterChipStrip
        availableCapabilityTags={["csv"]}
        availableAuditCharacteristics={[]}
        filters={{ ...ALL_OFF, capabilityTags: new Set(["csv"]) }}
        onChange={onChange}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /csv/i }));
    const updated: CatalogFilters = onChange.mock.calls[0][0];
    expect(updated.capabilityTags.has("csv")).toBe(false);
  });

  it("renders 'Clear filters' when any filter is active", () => {
    render(
      <FilterChipStrip
        availableCapabilityTags={["csv"]}
        availableAuditCharacteristics={[]}
        filters={{ ...ALL_OFF, capabilityTags: new Set(["csv"]) }}
        onChange={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: /clear filters/i })).toBeInTheDocument();
  });

  it("does not render 'Clear filters' when no filters are active", () => {
    render(
      <FilterChipStrip
        availableCapabilityTags={["csv"]}
        availableAuditCharacteristics={[]}
        filters={ALL_OFF}
        onChange={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: /clear filters/i })).not.toBeInTheDocument();
  });
});

describe("detail level (elspeth-8555a6a9e0)", () => {
  const audits = ["quarantine", "credentials", "external_call", "coerce", "non_deterministic"];
  const empty = { capabilityTags: new Set<string>(), auditCharacteristics: new Set<string>() };

  it("hides the Capability group and non-behavioural audit chips by default", () => {
    render(
      <FilterChipStrip
        availableCapabilityTags={["csv", "llm"]}
        availableAuditCharacteristics={audits}
        filters={empty}
        onChange={vi.fn()}
      />,
    );
    expect(screen.queryByText("Capability:")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "quarantines bad rows" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "needs credentials" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "network call" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "can coerce types" })).not.toBeInTheDocument();
  });

  it("shows everything with show_advanced on", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    render(
      <FilterChipStrip
        availableCapabilityTags={["csv", "llm"]}
        availableAuditCharacteristics={audits}
        filters={empty}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText("Capability:")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "can coerce types" })).toBeInTheDocument();
  });
});
