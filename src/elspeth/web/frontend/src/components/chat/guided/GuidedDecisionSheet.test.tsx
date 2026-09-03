import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoIdentifiersInDefaultDom } from "@/test/defaultDomPins";
import type { ChatTurn } from "@/types/guided";
import { GuidedDecisionSheet } from "./GuidedDecisionSheet";
import type { GuidedDecisionRow } from "./guidedDecisionStages";

const SOURCE_ID = "00000000-0000-4000-8000-00000000e001";

function chatTurn(overrides: Partial<ChatTurn> & { seq: number }): ChatTurn {
  return {
    role: "user",
    content: "read the pages CSV",
    step: "step_1_source",
    ts_iso: "2026-09-03T00:00:00Z",
    assistant_message_kind: null,
    synthetic_failure_reason: null,
    turn_token: null,
    ...overrides,
  };
}

function renderSheet(
  props: Partial<React.ComponentProps<typeof GuidedDecisionSheet>> = {},
) {
  const onClose = vi.fn();
  const rows: readonly GuidedDecisionRow[] = [
    { key: SOURCE_ID, name: "pages", plugin: "csv_file" },
  ];
  const view = render(
    <GuidedDecisionSheet
      id="decision-sheet"
      stage="step_1_source"
      rows={rows}
      chatTurns={[chatTurn({ seq: 1 })]}
      record={null}
      onClose={onClose}
      {...props}
    />,
  );
  return { ...view, onClose };
}

describe("GuidedDecisionSheet", () => {
  it("names the stage it records as a region", () => {
    renderSheet();
    const region = screen.getByRole("region", { name: "Source — decided" });
    expect(region).toHaveAttribute("id", "decision-sheet");
  });

  it("names each settled component by its plugin's display name, never its id", () => {
    // The sheet is a decision record for a person, so it carries identity +
    // display only: a stable_id is forensic data that belongs nowhere on it.
    const { container } = renderSheet();
    expect(screen.getByText("pages")).toBeInTheDocument();
    expect(screen.getByText("CSV File")).toBeInTheDocument();
    expect(container.textContent).not.toContain(SOURCE_ID);
    expectNoIdentifiersInDefaultDom(container);
  });

  it("names a component whose id IS its plugin exactly once", () => {
    // "Select Columns · Select Columns" is noise, not identification — and it
    // is the COMMON shape on the transforms sheet, where a node's
    // author-chosen id is very often just its plugin's name.
    renderSheet({
      stage: "step_3_transforms",
      rows: [{ key: "select_columns", name: "Select Columns", plugin: "select_columns" }],
      chatTurns: [],
    });
    expect(screen.getByText("Select Columns")).toBeInTheDocument();
  });

  it("renders a plugin-less component as its name alone", () => {
    // A structural node carries no plugin; a row that printed the missing
    // value would trail off into "name · null".
    const { container } = renderSheet({
      stage: "step_3_transforms",
      rows: [{ key: "fan_out", name: "Fan Out", plugin: null }],
      chatTurns: [],
    });
    expect(screen.getByText("Fan Out")).toBeInTheDocument();
    expect(container.textContent).not.toContain("null");
  });

  it("replays the stage's turns as settled history, NOT a second live log", () => {
    // The sheet mounts outside the transcript, but its turns are still over:
    // a role=log here would announce a conversation that cannot change, and
    // would double-announce anything the transcript already carries.
    const { container } = renderSheet();
    expect(
      screen.getByRole("group", { name: "Guided build conversation" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("log")).toBeNull();
    expect(container.querySelector("[aria-live]")).toBeNull();
  });

  it("shows the stage's own decision record when it has one", () => {
    renderSheet({
      stage: "step_4_wire",
      rows: [],
      chatTurns: [],
      record: "Guided pipeline wiring confirmed.",
    });
    expect(
      screen.getByText("Guided pipeline wiring confirmed."),
    ).toBeInTheDocument();
  });

  it("says so rather than rendering a bare heading when a stage recorded nothing", () => {
    renderSheet({ rows: [], chatTurns: [], record: null });
    expect(
      screen.getByText("Nothing was recorded at this stage."),
    ).toBeInTheDocument();
  });

  it("takes focus on open so a keyboard user lands in what they asked for", () => {
    renderSheet();
    expect(screen.getByRole("region", { name: "Source — decided" })).toHaveFocus();
  });

  it("re-focuses when the caller swaps the sheet to another stage", () => {
    // Opening a SECOND tick while one sheet is open re-renders this component
    // with a new stage rather than remounting it — from the user's point of
    // view that is an open, and it must land focus like one.
    const { rerender } = renderSheet();
    screen.getByRole("button", { name: "Close" }).focus();
    rerender(
      <GuidedDecisionSheet
        id="decision-sheet"
        stage="step_2_sink"
        rows={[]}
        chatTurns={[]}
        record={null}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByRole("region", { name: "Output — decided" })).toHaveFocus();
  });

  it("closes from the Close button and from Escape", async () => {
    const user = userEvent.setup();
    const { onClose } = renderSheet();
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("offers NO way to change a settled component (lane E2 owns the rewind)", () => {
    // Deliberate scope pin, not an oversight: whether a stage rewind is a
    // literal session fork or a superseded proposal on a new composition
    // version is an open operator decision, and this read-only landing must
    // not ship a control that presumes an answer.
    renderSheet();
    expect(screen.queryByRole("button", { name: /change/i })).toBeNull();
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });
});
