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

function installPointerCapture(
  separator: HTMLElement,
  captured = true,
): {
  setPointerCapture: ReturnType<typeof vi.fn>;
  releasePointerCapture: ReturnType<typeof vi.fn>;
} {
  const setPointerCapture = vi.fn();
  const releasePointerCapture = vi.fn();
  separator.setPointerCapture = setPointerCapture;
  separator.hasPointerCapture = vi.fn(() => captured);
  separator.releasePointerCapture = releasePointerCapture;
  return { setPointerCapture, releasePointerCapture };
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
    const { setPointerCapture, releasePointerCapture } =
      installPointerCapture(separator);

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
    installPointerCapture(separator);

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
    installPointerCapture(separator);

    fireEvent.pointerDown(separator, { pointerId: 9, clientX: 300 });
    fireEvent.pointerMove(separator, { pointerId: 9, clientX: 260 });
    fireEvent.pointerCancel(separator, { pointerId: 9 });

    expect(onResize).toHaveBeenCalledExactlyOnceWith(380);
    expect(onResizeEnd).toHaveBeenCalledExactlyOnceWith(380);
  });

  it("reclamps a published drag when the maximum shrinks before pointer-up", () => {
    const raf = installRafHarness();
    const onResize = vi.fn();
    const onResizeEnd = vi.fn();
    const view = render(
      <WorkspaceSeparator
        value={420}
        min={360}
        max={640}
        disabled={false}
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    const separator = screen.getByRole("separator", {
      name: "Resize authoring pane",
    });
    separator.setPointerCapture = vi.fn();
    separator.hasPointerCapture = vi.fn(() => true);
    separator.releasePointerCapture = vi.fn();

    fireEvent.pointerDown(separator, { pointerId: 10, clientX: 100 });
    fireEvent.pointerMove(separator, { pointerId: 10, clientX: 400 });
    act(() => raf.flush());
    expect(onResize.mock.calls).toEqual([[640]]);

    view.rerender(
      <WorkspaceSeparator
        value={640}
        min={360}
        max={460}
        disabled={false}
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    fireEvent.pointerUp(separator, { pointerId: 10, clientX: 400 });

    expect(onResize.mock.calls).toEqual([[640], [460]]);
    expect(onResizeEnd).toHaveBeenCalledExactlyOnceWith(460);
    expect(separator.releasePointerCapture).toHaveBeenCalledWith(10);
  });

  it("cancels a queued frame and reclamps its width when bounds change", () => {
    const raf = installRafHarness();
    const onResize = vi.fn();
    const onResizeEnd = vi.fn();
    const view = render(
      <WorkspaceSeparator
        value={420}
        min={360}
        max={640}
        disabled={false}
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    const separator = screen.getByRole("separator", {
      name: "Resize authoring pane",
    });
    separator.setPointerCapture = vi.fn();
    separator.hasPointerCapture = vi.fn(() => true);
    separator.releasePointerCapture = vi.fn();

    fireEvent.pointerDown(separator, { pointerId: 11, clientX: 100 });
    fireEvent.pointerMove(separator, { pointerId: 11, clientX: 20 });
    expect(raf.queuedCount()).toBe(1);

    view.rerender(
      <WorkspaceSeparator
        value={420}
        min={500}
        max={640}
        disabled={false}
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );

    expect(raf.queuedCount()).toBe(0);
    act(() => raf.flush());
    expect(onResize).not.toHaveBeenCalled();

    fireEvent.pointerUp(separator, { pointerId: 11, clientX: 20 });
    expect(onResize).toHaveBeenCalledExactlyOnceWith(500);
    expect(onResizeEnd).toHaveBeenCalledExactlyOnceWith(500);
  });

  it("finalizes a queued drag after disable without a stale frame publication", () => {
    const raf = installRafHarness();
    const onResize = vi.fn();
    const onResizeEnd = vi.fn();
    const view = render(
      <WorkspaceSeparator
        value={420}
        min={360}
        max={640}
        disabled={false}
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    const separator = screen.getByRole("separator", {
      name: "Resize authoring pane",
    });
    separator.setPointerCapture = vi.fn();
    separator.hasPointerCapture = vi.fn(() => true);
    separator.releasePointerCapture = vi.fn();

    fireEvent.pointerDown(separator, { pointerId: 12, clientX: 100 });
    fireEvent.pointerMove(separator, { pointerId: 12, clientX: 180 });
    expect(raf.queuedCount()).toBe(1);

    view.rerender(
      <WorkspaceSeparator
        value={420}
        min={360}
        max={640}
        disabled
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );

    expect(raf.queuedCount()).toBe(0);
    act(() => raf.flush());
    expect(onResize).not.toHaveBeenCalled();

    fireEvent.pointerUp(separator, { pointerId: 12, clientX: 180 });
    expect(onResize).toHaveBeenCalledExactlyOnceWith(500);
    expect(onResizeEnd).toHaveBeenCalledExactlyOnceWith(500);
    expect(separator.releasePointerCapture).toHaveBeenCalledWith(12);
    act(() => raf.flush());
    expect(onResize).toHaveBeenCalledTimes(1);
  });

  it("cleans up pointer-cancel after disable when capture was implicitly lost", () => {
    const raf = installRafHarness();
    const onResize = vi.fn();
    const onResizeEnd = vi.fn();
    const view = render(
      <WorkspaceSeparator
        value={420}
        min={360}
        max={640}
        disabled={false}
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    const separator = screen.getByRole("separator", {
      name: "Resize authoring pane",
    });
    separator.setPointerCapture = vi.fn();
    separator.hasPointerCapture = vi.fn(() => false);
    separator.releasePointerCapture = vi.fn(() => {
      throw new DOMException("capture already released", "NotFoundError");
    });

    fireEvent.pointerDown(separator, { pointerId: 13, clientX: 300 });
    fireEvent.pointerMove(separator, { pointerId: 13, clientX: 260 });
    view.rerender(
      <WorkspaceSeparator
        value={420}
        min={360}
        max={640}
        disabled
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );

    expect(() => {
      fireEvent.pointerCancel(separator, { pointerId: 13 });
    }).not.toThrow();
    expect(raf.queuedCount()).toBe(0);
    expect(onResize).toHaveBeenCalledExactlyOnceWith(380);
    expect(onResizeEnd).toHaveBeenCalledExactlyOnceWith(380);
    expect(separator.releasePointerCapture).not.toHaveBeenCalled();

    fireEvent.pointerCancel(separator, { pointerId: 13 });
    expect(onResizeEnd).toHaveBeenCalledTimes(1);
  });

  it("ignores post-disable movement before pointer-up finalizes the pre-disable width", () => {
    const raf = installRafHarness();
    const onResize = vi.fn();
    const onResizeEnd = vi.fn();
    const view = render(
      <WorkspaceSeparator
        value={420}
        min={360}
        max={640}
        disabled={false}
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    const separator = screen.getByRole("separator", {
      name: "Resize authoring pane",
    });
    const { releasePointerCapture } = installPointerCapture(separator);

    fireEvent.pointerDown(separator, { pointerId: 14, clientX: 100 });
    fireEvent.pointerMove(separator, { pointerId: 14, clientX: 160 });
    expect(raf.queuedCount()).toBe(1);

    view.rerender(
      <WorkspaceSeparator
        value={420}
        min={360}
        max={640}
        disabled
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    fireEvent.pointerMove(separator, { pointerId: 14, clientX: 320 });
    fireEvent.pointerUp(separator, { pointerId: 14, clientX: 320 });

    expect(onResize.mock.calls).toEqual([[480]]);
    expect(onResizeEnd).toHaveBeenCalledExactlyOnceWith(480);
    expect(releasePointerCapture).toHaveBeenCalledWith(14);
    expect(raf.queuedCount()).toBe(0);
    act(() => raf.flush());
    expect(onResize.mock.calls).toEqual([[480]]);
  });

  it("ignores post-disable movement before pointer-cancel finalizes at current bounds", () => {
    const raf = installRafHarness();
    const onResize = vi.fn();
    const onResizeEnd = vi.fn();
    const view = render(
      <WorkspaceSeparator
        value={420}
        min={360}
        max={640}
        disabled={false}
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    const separator = screen.getByRole("separator", {
      name: "Resize authoring pane",
    });
    const { releasePointerCapture } = installPointerCapture(separator);

    fireEvent.pointerDown(separator, { pointerId: 15, clientX: 100 });
    fireEvent.pointerMove(separator, { pointerId: 15, clientX: 300 });
    expect(raf.queuedCount()).toBe(1);

    view.rerender(
      <WorkspaceSeparator
        value={420}
        min={360}
        max={460}
        disabled
        onResize={onResize}
        onResizeEnd={onResizeEnd}
      />,
    );
    fireEvent.pointerMove(separator, { pointerId: 15, clientX: 20 });
    fireEvent.pointerCancel(separator, { pointerId: 15 });

    expect(onResize.mock.calls).toEqual([[460]]);
    expect(onResizeEnd).toHaveBeenCalledExactlyOnceWith(460);
    expect(releasePointerCapture).toHaveBeenCalledWith(15);
    expect(raf.queuedCount()).toBe(0);
    act(() => raf.flush());
    expect(onResize.mock.calls).toEqual([[460]]);
  });
});
