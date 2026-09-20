import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GeneratedPassword } from "./GeneratedPassword";
import { COPIED_RESET_MS } from "./peoplePanel";

describe("GeneratedPassword", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
  });
  afterEach(() => vi.useRealTimers());

  it("offers Copy again a few seconds after a copy, instead of reading Copied for ever", async () => {
    render(<GeneratedPassword credential={{ username: "alex", password: "gen-Pass-1", cause: "created" }} onDismiss={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy password" }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(COPIED_RESET_MS - 1); });
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1); });
    expect(screen.getByRole("button", { name: "Copy password" })).toBeInTheDocument();
    // The password itself is untouched: it is still the one-time value.
    expect(screen.getByTestId("generated-password")).toHaveTextContent("gen-Pass-1");
  });
});
