import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkspaceSeparator } from "./WorkspaceSeparator";

interface RafHarness {
  flush: () => void;
  queuedCount: () => number;
}

function installRafHarness(): RafHarness {
  let nextId = 1;
  const callbacks = new Map<number, FrameRequestCallback>();
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    const id = nextId++;
    callbacks.set(id, callback);
    return id;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => {
    callbacks.delete(id);
  });
  return {
    flush: () => {
      const queued = [...callbacks.values()];
      callbacks.clear();
      queued.forEach((callback) => callback(0));
    },
    queuedCount: () => callbacks.size,
  };
}

function renderSeparator(overrides: {
  value?: number;
  min?: number;
  max?: number;
  disabled?: boolean;
} = {}) {
  const onResize = vi.fn();
  const onResizeEnd = vi.fn();
  render(
    <WorkspaceSeparator
      value={overrides.value ?? 420}
      min={overrides.min ?? 360}
      max={overrides.max ?? 640}
      disabled={overrides.disabled ?? false}
      onResize={onResize}
      onResizeEnd={onResizeEnd}
    />,
  );
  return {
    separator: screen.getByRole("separator", {
      name: "Resize authoring pane",
      hidden: overrides.disabled,
    }),
    onResize,
    onResizeEnd,
  };
}

describe("WorkspaceSeparator", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("publishes the vertical separator range and remains keyboard focusable", () => {
    const { separator } = renderSeparator();

    expect(separator).toHaveAttribute("aria-orientation", "vertical");
    expect(separator).toHaveAttribute("aria-valuemin", "360");
    expect(separator).toHaveAttribute("aria-valuemax", "640");
    expect(separator).toHaveAttribute("aria-valuenow", "420");
    expect(separator).toHaveAttribute("tabindex", "0");
  });

  it("resizes with arrows, Shift, Home, and End using explicit final widths", async () => {
    const user = userEvent.setup();
    const { separator, onResize, onResizeEnd } = renderSeparator();

    separator.focus();
    await user.keyboard("{ArrowRight}");
    await user.keyboard("{Shift>}{ArrowRight}{/Shift}");
    await user.keyboard("{ArrowLeft}");
    await user.keyboard("{Shift>}{ArrowLeft}{/Shift}");
    await user.keyboard("{Home}");
    await user.keyboard("{End}");

    expect(onResize.mock.calls).toEqual([
      [436],
      [484],
      [468],
      [420],
      [360],
      [640],
    ]);
    expect(onResizeEnd.mock.calls).toEqual(onResize.mock.calls);
  });

  it("clamps every keyboard operation", () => {
    const { separator, onResize, onResizeEnd } = renderSeparator({
      value: 635,
    });

    fireEvent.keyDown(separator, { key: "ArrowRight", shiftKey: true });
    fireEvent.keyDown(separator, { key: "ArrowRight" });
    fireEvent.keyDown(separator, { key: "Home" });
    fireEvent.keyDown(separator, { key: "ArrowLeft" });

    expect(onResize.mock.calls).toEqual([[640], [640], [360], [360]]);
    expect(onResizeEnd.mock.calls).toEqual(onResize.mock.calls);
  });

  it("is inert and unfocusable when disabled", () => {
    const { separator, onResize, onResizeEnd } = renderSeparator({
      disabled: true,
    });

    expect(separator).toHaveAttribute("tabindex", "-1");
    expect(separator).toHaveAttribute("aria-disabled", "true");
    fireEvent.keyDown(separator, { key: "End" });
    fireEvent.pointerDown(separator, { pointerId: 4, clientX: 100 });
    fireEvent.pointerMove(separator, { pointerId: 4, clientX: 200 });
    fireEvent.pointerUp(separator, { pointerId: 4, clientX: 200 });

    expect(onResize).not.toHaveBeenCalled();
    expect(onResizeEnd).not.toHaveBeenCalled();
  });

  it("captures the pointer and batches moves to the latest clamped width", () => {
    const raf = installRafHarness();
    const { separator, onResize, onResizeEnd } = renderSeparator();
    const setPointerCapture = vi.fn();
    const releasePointerCapture = vi.fn();
    separator.setPointerCapture = setPointerCapture;
    separator.releasePointerCapture = releasePointerCapture;

    fireEvent.pointerDown(separator, { pointerId: 7, clientX: 100 });
    fireEvent.pointerMove(separator, { pointerId: 7, clientX: 120 });
    fireEvent.pointerMove(separator, { pointerId: 7, clientX: 800 });

    expect(setPointerCapture).toHaveBeenCalledWith(7);
    expect(raf.queuedCount()).toBe(1);
    expect(onResize).not.toHaveBeenCalled();

    act(() => raf.flush());
    expect(onResize).toHaveBeenCalledTimes(1);
    expect(onResize).toHaveBeenLastCalledWith(640);

    fireEvent.pointerUp(separator, { pointerId: 7, clientX: 800 });
    expect(onResizeEnd).toHaveBeenCalledExactlyOnceWith(640);
    expect(releasePointerCapture).toHaveBeenCalledWith(7);
  });

  it("flushes a queued final move on pointer-up without reading stale React state", () => {
    const raf = installRafHarness();
    const { separator, onResize, onResizeEnd } = renderSeparator();
    separator.setPointerCapture = vi.fn();
    separator.releasePointerCapture = vi.fn();

    fireEvent.pointerDown(separator, { pointerId: 2, clientX: 100 });
    fireEvent.pointerMove(separator, { pointerId: 2, clientX: 164 });
    expect(raf.queuedCount()).toBe(1);

    fireEvent.pointerUp(separator, { pointerId: 2, clientX: 164 });

    expect(raf.queuedCount()).toBe(0);
    expect(onResize).toHaveBeenCalledExactlyOnceWith(484);
    expect(onResizeEnd).toHaveBeenCalledExactlyOnceWith(484);
  });

  it("commits the latest explicit width on pointer-cancel", () => {
    installRafHarness();
    const { separator, onResize, onResizeEnd } = renderSeparator();
    separator.setPointerCapture = vi.fn();
    separator.releasePointerCapture = vi.fn();

    fireEvent.pointerDown(separator, { pointerId: 9, clientX: 300 });
    fireEvent.pointerMove(separator, { pointerId: 9, clientX: 260 });
    fireEvent.pointerCancel(separator, { pointerId: 9 });

    expect(onResize).toHaveBeenCalledExactlyOnceWith(380);
    expect(onResizeEnd).toHaveBeenCalledExactlyOnceWith(380);
  });
});
