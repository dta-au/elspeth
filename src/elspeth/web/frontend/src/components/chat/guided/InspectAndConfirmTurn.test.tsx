import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InspectAndConfirmTurn } from "./InspectAndConfirmTurn";
import { nullResponse } from "@/test/guided-fixtures";
import type { InspectAndConfirmPayload } from "@/types/guided";

const PAYLOAD: InspectAndConfirmPayload = {
  observed: {
    columns: ["id", "value"],
    samples: [{ id: "one", value: 42 }, { id: "two" }],
    warnings: ["Some values are missing."],
  },
};

describe("InspectAndConfirmTurn", () => {
  it("renders inspected columns and sparse sample values", () => {
    render(<InspectAndConfirmTurn payload={PAYLOAD} onSubmit={vi.fn()} />);
    expect(screen.getAllByRole("columnheader").map((header) => header.textContent)).toEqual(["id", "value"]);
    expect(screen.getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["one", "42", "two", ""]);
    expect(screen.getByLabelText("Data warnings")).toHaveTextContent("Some values are missing.");
  });

  it("explains redacted samples without showing an empty warnings panel", () => {
    render(<InspectAndConfirmTurn payload={{ observed: { columns: ["id"], samples: [], warnings: [] } }} onSubmit={vi.fn()} />);
    expect(screen.getByText(/Row contents aren't previewed here/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Data warnings")).not.toBeInTheDocument();
  });

  it.each([false, true])("offers factual confirmation and provider-mediated changes (tutorial=%s)", (isTutorial) => {
    render(<InspectAndConfirmTurn payload={PAYLOAD} onSubmit={vi.fn()} isTutorial={isTutorial} />);
    expect(screen.queryByRole("button", { name: "Edit columns..." })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByText(/ask the composer.*processing step/i)).toBeInTheDocument();
  });

  it("confirms only the exact observed columns, never source options, samples or warnings", async () => {
    const onSubmit = vi.fn();
    render(<InspectAndConfirmTurn payload={PAYLOAD} onSubmit={onSubmit} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Looks right" }));
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith({
      ...nullResponse(),
      chosen: null,
      custom_inputs: null,
      edited_values: { columns: ["id", "value"] },
    });
  });

  it("cannot confirm while disabled", async () => {
    const onSubmit = vi.fn();
    render(<InspectAndConfirmTurn payload={PAYLOAD} onSubmit={onSubmit} disabled />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Looks right" }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("keeps warning ids isolated for simultaneous instances", () => {
    render(<><InspectAndConfirmTurn payload={PAYLOAD} onSubmit={vi.fn()} /><InspectAndConfirmTurn payload={PAYLOAD} onSubmit={vi.fn()} /></>);
    const warnings = screen.getAllByLabelText("Data warnings");
    expect(warnings[0].id).not.toBe(warnings[1].id);
  });
});
