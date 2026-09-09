import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ValidationResultBanner } from "./ValidationResult";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";
import { UNKNOWN_COMPONENT_PHRASE } from "@/components/chat/guided/pipelineGloss";
import type { NodeSpec, ValidationResult } from "@/types/index";

const READY_READINESS = {
  authoring_valid: true,
  execution_ready: true,
  completion_ready: true,
  blockers: [],
};

function makePassResult(overrides: Partial<ValidationResult> = {}): ValidationResult {
  return {
    is_valid: true,
    summary: "Validation passed",
    checks: [
      {
        name: "plugin_enablement",
        passed: true,
        detail: "plugin_enablement passed.",
        affected_nodes: [],
        outcome_code: null,
      },
      {
        name: "graph_structure",
        passed: true,
        detail: "Graph structure is valid",
        affected_nodes: [],
        outcome_code: null,
      },
    ],
    errors: [],
    warnings: [],
    readiness: READY_READINESS,
    ...overrides,
  };
}

describe("ValidationResultBanner", () => {
  // Review round 1, Important-1: this describe block has no store reset, so
  // the two tests below that set showAdvanced:true were leaking it into
  // every later test in the file — the two forced-advisory-visibility tests
  // that follow (identity_node_advisory, advisor_signoff) were incidentally
  // passing via the FULL-LIST branch instead of the forced-guidance branch
  // they exist to cover, since showAdvanced never went back to false.
  afterEach(() => {
    resetStore(usePreferencesStore);
  });

  it("collapses a clean pass to a one-line summary without check details", () => {
    render(<ValidationResultBanner result={makePassResult()} />);

    const summaryBtn = screen.getByRole("button", {
      name: /validation passed\. show details\./i,
    });
    expect(summaryBtn).toHaveAttribute("aria-expanded", "false");
    expect(summaryBtn).toHaveTextContent("2 checks");
    expect(screen.queryByText(/plugin_enablement passed/)).toBeNull();
  });

  it("expands to check details on click and collapses again via Collapse", async () => {
    usePreferencesStore.setState({ showAdvanced: true });
    const user = userEvent.setup();
    render(<ValidationResultBanner result={makePassResult()} />);

    await user.click(
      screen.getByRole("button", { name: /validation passed\. show details\./i }),
    );

    expect(screen.getByText(/plugin_enablement passed/)).toBeInTheDocument();
    expect(screen.getByText(/Graph structure is valid/)).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /collapse validation details/i }),
    );

    expect(screen.queryByText(/plugin_enablement passed/)).toBeNull();
    expect(
      screen.getByRole("button", { name: /validation passed\. show details\./i }),
    ).toBeInTheDocument();
  });

  it("auto-expands when the pass carries warnings and offers no Collapse", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    render(
      <ValidationResultBanner
        result={makePassResult({
          warnings: [
            {
              component_id: "csv_source",
              component_type: "source",
              message: "Batch size is small",
              suggestion: "Consider increasing batch size",
            },
          ],
        })}
      />,
    );

    expect(screen.getByText(/Batch size is small/)).toBeInTheDocument();
    expect(screen.getByText(/plugin_enablement passed/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /collapse validation details/i }),
    ).toBeNull();
  });

  it("auto-expands when a passed check carries identity_node_advisory guidance and offers no Collapse", () => {
    // Mixed checks array (review round 1, Important-1): an ordinary passing
    // check (graph_structure) rides alongside the advisory one specifically
    // so this test can DISCRIMINATE the forced-guidance branch from the
    // full-list branch — the two render identical text for a solo advisory
    // check, since showAdvanced leaking true would ALSO show the advisory
    // detail (just via the other branch). The forced branch renders only
    // [...advisoryChecks, ...failedAdvisorChecks]; the full-list branch
    // renders every check. So with showAdvanced genuinely false (the
    // describe block's afterEach reset keeps it that way), graph_structure's
    // detail must be ABSENT — if it were present, showAdvanced leaked true
    // and this is exercising the wrong branch.
    render(
      <ValidationResultBanner
        result={makePassResult({
          warnings: [],
          checks: [
            {
              name: "identity_node_advisory",
              passed: true,
              detail:
                "Node 'passthrough_1' is an identity-shaped passthrough between 'scrape' and sink 'csv_out'. Consider removing it and wiring 'scrape'.on_success directly to 'csv_out'.",
              affected_nodes: ["passthrough_1"],
              outcome_code: null,
            },
            {
              name: "graph_structure",
              passed: true,
              detail: "Graph structure is valid",
              affected_nodes: [],
              outcome_code: null,
            },
          ],
        })}
      />,
    );

    // The "name: detail" prefix only the forced-guidance list renders (the
    // full list drops it for every check but advisor_signoff) — a second,
    // stronger pin that this specific row came from the forced branch. The
    // detail's own text starts with "Node 'passthrough_1' is an..."; "Consider
    // removing it" is a later sentence within it, not the start.
    expect(
      screen.getByText(/identity_node_advisory: Node 'passthrough_1'/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Consider removing it/)).toBeInTheDocument();
    expect(screen.queryByText("Graph structure is valid")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /collapse validation details/i }),
    ).toBeNull();
  });

  it("auto-expands a failed completion advisory check without exposing its compatibility key", () => {
    // Mixed checks array (review round 1, Important-1): advisor_signoff gets
    // the SAME prefixed "Completion advisory review: ..." text from EITHER
    // branch (the full list special-cases it identically to the forced
    // list), so its own text can't tell the two branches apart. An ordinary
    // passing check (graph_structure) alongside it can: the forced branch
    // never renders it, so its presence would mean showAdvanced leaked true.
    render(
      <ValidationResultBanner
        result={makePassResult({
          checks: [
            {
              name: "advisor_signoff",
              passed: false,
              detail:
                "The evidence-scoped completion advisory review has not covered this pipeline version.",
              affected_nodes: [],
              outcome_code: null,
            },
            {
              name: "graph_structure",
              passed: true,
              detail: "Graph structure is valid",
              affected_nodes: [],
              outcome_code: null,
            },
          ],
          readiness: {
            authoring_valid: true,
            execution_ready: true,
            completion_ready: false,
            blockers: [
              {
                code: "advisor_signoff_blocked",
                component_id: "pipeline",
                component_type: "pipeline",
                detail:
                  "The evidence-scoped completion advisory review has not covered this pipeline version.",
              },
            ],
          },
        })}
      />,
    );

    expect(
      screen.getByText(/Completion advisory review: The evidence-scoped/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/advisor_signoff:/)).toBeNull();
    // Discriminates the forced branch from the full-list branch (see comment
    // above): only the full list would render this ordinary check's detail.
    expect(screen.queryByText("Graph structure is valid")).not.toBeInTheDocument();
    // Pins the honest partial-count wording (review round 2): 1 of these 2
    // checks (graph_structure) actually passed, so the standard-view summary
    // must say so rather than the "All 2 checks passed." it would read if it
    // counted result.checks.length alone without checking check.passed.
    expect(screen.getByText("1 of 2 checks passed.")).toBeInTheDocument();
    expect(screen.queryByText("All 2 checks passed.")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /validation passed\. show details\./i }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: /collapse validation details/i }),
    ).toBeNull();
  });

  it("renders failures expanded with per-component errors, unchanged", () => {
    render(
      <ValidationResultBanner
        result={makePassResult({
          is_valid: false,
          summary: "Validation failed",
          errors: [
            {
              component_id: "select_columns",
              component_type: "transform",
              message: "Bad transform",
              suggestion: "Choose a supported projection.",
            },
          ],
          readiness: {
            ...READY_READINESS,
            authoring_valid: false,
            execution_ready: false,
            completion_ready: false,
          },
        })}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Bad transform");
    expect(screen.getByText(/Choose a supported projection/)).toBeInTheDocument();
  });

  // elspeth-7bcc3d5233. The suggestion used to be built INTO the navigable
  // button, so a block <div> sat inside a <button> (invalid flow content), the
  // button's underline ran across the helper note, and the whole four-line
  // block was one hit target. These pin the CONTAINMENT that causes those
  // symptoms — jsdom cannot measure an underline or a list marker, so the
  // mechanism is what is asserted: the suggestion is a sibling of the button
  // inside the same <li>, and only the button navigates.
  it("renders an error suggestion as a sibling of the navigable button, not inside it", async () => {
    const user = userEvent.setup();
    const onComponentClick = vi.fn();
    render(
      <ValidationResultBanner
        result={makePassResult({
          is_valid: false,
          summary: "Validation failed",
          errors: [
            {
              component_id: "select_columns",
              component_type: "transform",
              message: "Bad transform",
              suggestion: "Choose a supported projection.",
            },
          ],
        })}
        componentNames={{ select_columns: "Select columns" }}
        onComponentClick={onComponentClick}
      />,
    );

    const button = screen.getByRole("button", { name: /Bad transform/ });
    const suggestion = screen.getByText(/Choose a supported projection/);

    expect(button).toHaveClass("validation-banner-component-btn--error");
    expect(button.contains(suggestion)).toBe(false);
    expect(button.querySelector("div")).toBeNull();
    expect(suggestion.closest("li")).toBe(button.closest("li"));
    expect(suggestion).toHaveClass("validation-banner-suggestion");

    await user.click(suggestion);
    expect(onComponentClick).not.toHaveBeenCalled();

    await user.click(button);
    expect(onComponentClick).toHaveBeenCalledExactlyOnceWith("select_columns");
  });

  it("renders a warning suggestion as a sibling of the navigable button, not inside it", async () => {
    const user = userEvent.setup();
    const onComponentClick = vi.fn();
    render(
      <ValidationResultBanner
        result={makePassResult({
          warnings: [
            {
              component_id: "csv_source",
              component_type: "source",
              message: "Batch size is small",
              suggestion: "Consider increasing batch size",
            },
          ],
        })}
        componentNames={{ csv_source: "CSV source" }}
        onComponentClick={onComponentClick}
      />,
    );

    const button = screen.getByRole("button", { name: /Batch size is small/ });
    const suggestion = screen.getByText(/Consider increasing batch size/);

    expect(button).toHaveClass("validation-banner-component-btn--warning");
    expect(button.contains(suggestion)).toBe(false);
    expect(button.querySelector("div")).toBeNull();
    expect(suggestion.closest("li")).toBe(button.closest("li"));
    expect(suggestion).toHaveClass("validation-banner-suggestion");

    await user.click(suggestion);
    expect(onComponentClick).not.toHaveBeenCalled();

    await user.click(button);
    expect(onComponentClick).toHaveBeenCalledExactlyOnceWith("csv_source");
  });

  it("still renders a suggestion when the component is not navigable", () => {
    render(
      <ValidationResultBanner
        result={makePassResult({
          is_valid: false,
          summary: "Validation failed",
          errors: [
            {
              component_id: "unknown_component",
              component_type: "transform",
              message: "Bad transform",
              suggestion: "Choose a supported projection.",
            },
          ],
        })}
      />,
    );

    expect(screen.queryByRole("button")).toBeNull();
    expect(
      screen.getByText(/Choose a supported projection/),
    ).toHaveClass("validation-banner-suggestion");
  });
});

describe("ValidationResultBanner detail level (elspeth-27efd1e801)", () => {
  beforeEach(() => {
    resetStore(usePreferencesStore);
    useSessionStore.setState({ compositionState: null });
  });

  it("keeps the per-stage check list out of the expanded pass view by default", async () => {
    const user = userEvent.setup();
    render(<ValidationResultBanner result={makePassResult()} />);
    await user.click(screen.getByRole("button", { name: "Validation passed. Show details." }));
    expect(screen.queryByText(/plugin_enablement/)).not.toBeInTheDocument();
    expect(screen.getByText("All 2 checks passed.")).toBeInTheDocument();
  });

  it("shows the check list, without the check-name prefix, once show_advanced flips on a mounted banner", async () => {
    const user = userEvent.setup();
    render(<ValidationResultBanner result={makePassResult()} />);
    await user.click(screen.getByRole("button", { name: "Validation passed. Show details." }));
    expect(screen.queryByText("Graph structure is valid")).not.toBeInTheDocument();
    act(() => usePreferencesStore.setState({ showAdvanced: true }));
    const item = screen.getByText("Graph structure is valid");
    expect(item).toBeInTheDocument();
    expect(item.closest("li")).toHaveAttribute("title", "graph_structure");
    expect(screen.queryByText(/^graph_structure:/)).not.toBeInTheDocument();
  });

  it("humanises a contract-violation dump into a headline and keeps the raw text behind Technical details", () => {
    render(
      <ValidationResultBanner
        result={makePassResult({
          is_valid: false,
          errors: [
            {
              component_id: "assess",
              component_type: "transform",
              message:
                "Schema contract violation: 'source' -> 'assess': required field 'case_study1' is not guaranteed by the producer",
              suggestion: null,
            },
          ],
        })}
      />,
    );
    // role="alert" (ValidationResult.tsx:236) wraps the whole error list, so
    // the raw text IS inside the alert — the contract is that it sits only
    // inside a closed <details>, never in the headline the AT announces first.
    const alert = screen.getByRole("alert");
    const raw = within(alert).getByText(/Schema contract violation/);
    const details = raw.closest("details");
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    expect(within(details as HTMLElement).getByText("Technical details")).toBeInTheDocument();
    const item = alert.querySelector("li.validation-banner-error-item") as HTMLElement;
    const headline = Array.from(item.childNodes)
      .filter((node) => (node as HTMLElement).tagName !== "DETAILS")
      .map((node) => node.textContent)
      .join("");
    expect(headline).not.toMatch(/Schema contract violation/);
    // No nodes/componentNames context is passed, so "assess" cannot resolve
    // to anything nicer — the honest fallback is the shared generic phrase,
    // never the raw id itself (elspeth-93f5621f18: this line used to assert
    // the opposite, pinning the bug this ticket fixes).
    expect(headline).not.toMatch(/\bassess\b/);
    expect(headline).toMatch(/this step/i);
  });

  it("never renders a bare component id when the banner has no nodes list (elspeth-93f5621f18)", () => {
    useSessionStore.setState({ compositionState: null });
    render(
      <ValidationResultBanner
        result={{ is_valid: false, errors: [{ message: "Field missing", component_id: "select_columns", component_type: "transform" }], warnings: [], checks: [] } as unknown as ValidationResult}
      />,
    );
    expect(screen.queryByText(/select_columns/)).not.toBeInTheDocument();
    // The name is the author's own word for the step, title-cased — NOT
    // UNKNOWN_COMPONENT_PHRASE, which is identical for every component and so
    // collapsed two distinct errors into one indistinguishable pair (ux M-1).
    // The raw id stays out of visible text and lives in the identifier
    // channels instead.
    const prefix = screen.getByText("Select Columns:");
    expect(prefix).not.toHaveTextContent(UNKNOWN_COMPONENT_PHRASE);
    expect(prefix).toHaveAttribute("data-component-id", "select_columns");
    expect(prefix).toHaveAttribute("title", "select_columns");
  });

  it("keeps two errored components distinguishable and the type:id pair out of prose (ux M-1 / final-review M6)", () => {
    // Two halves of one rule. Without a nodes list both components resolved to
    // UNKNOWN_COMPONENT_PHRASE, so the banner said "this step" twice and the
    // reader could not tell which failed. WITH a nodes list a wired component
    // rendered its raw structural pair, "transform:select_columns", straight
    // into the bold prefix — the engineer register in the reader's slot.
    useSessionStore.setState({ compositionState: null });
    render(
      <ValidationResultBanner
        nodes={[
          { id: "select_columns", node_type: "transform", plugin: null, input: "raw_rows", on_success: null, on_error: null, options: {} },
          { id: "merge_results", node_type: "coalesce", plugin: null, input: "scored", on_success: null, on_error: null, options: {} },
        ] as unknown as NodeSpec[]}
        result={{
          is_valid: false,
          errors: [
            { message: "Field missing", component_id: "select_columns", component_type: "transform" },
            { message: "Branch missing", component_id: "merge_results", component_type: "coalesce" },
          ],
          warnings: [],
          checks: [],
        } as unknown as ValidationResult}
      />,
    );

    const alert = screen.getByRole("alert");
    // Distinct, and each names the component AND its kind in the reader
    // register — the `Name (Kind)` shape the consent dialog and Spec card use.
    const selectPrefix = within(alert).getByText("Select Columns (Transform):");
    const mergePrefix = within(alert).getByText("Merge Results (Coalesce):");
    // Pinned against the sentinel itself, not against the two names: the
    // defect was that BOTH collapsed to this one phrase.
    for (const prefix of [selectPrefix, mergePrefix]) {
      expect(prefix).not.toHaveTextContent(UNKNOWN_COMPONENT_PHRASE);
    }
    // The raw pair is reachable, but only through the identifier channels.
    expect(selectPrefix).toHaveAttribute("title", "transform:select_columns");
    expect(alert.querySelector('[data-component-id="coalesce:merge_results"]')).not.toBeNull();
    expect(alert).not.toHaveTextContent("transform:select_columns");
    expect(alert).not.toHaveTextContent("coalesce:merge_results");
  });
});
