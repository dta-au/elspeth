import {
  type KeyboardEvent,
  type PointerEvent,
  useEffect,
  useLayoutEffect,
  useRef,
} from "react";

export interface WorkspaceSeparatorProps {
  value: number;
  min: number;
  max: number;
  disabled: boolean;
  onResize: (value: number) => void;
  onResizeEnd: (finalWidth: number) => void;
}

interface PointerResize {
  pointerId: number;
  startX: number;
  startWidth: number;
  latestWidth: number;
  publishedWidth: number;
  queuedWidth: number | null;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function WorkspaceSeparator({
  value,
  min,
  max,
  disabled,
  onResize,
  onResizeEnd,
}: WorkspaceSeparatorProps) {
  const currentWidthRef = useRef(clamp(value, min, max));
  const pointerResizeRef = useRef<PointerResize | null>(null);
  const animationFrameRef = useRef<number | null>(null);

  useLayoutEffect(() => {
    if (pointerResizeRef.current === null) {
      currentWidthRef.current = clamp(value, min, max);
    }
  }, [max, min, value]);

  useEffect(
    () => () => {
      if (animationFrameRef.current !== null) {
        cancelAnimationFrame(animationFrameRef.current);
      }
    },
    [],
  );

  const publishKeyboardWidth = (nextWidth: number): void => {
    const finalWidth = clamp(nextWidth, min, max);
    currentWidthRef.current = finalWidth;
    onResize(finalWidth);
    onResizeEnd(finalWidth);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (disabled) return;
    const step = event.shiftKey ? 48 : 16;
    let nextWidth: number;
    switch (event.key) {
      case "ArrowLeft":
        nextWidth = currentWidthRef.current - step;
        break;
      case "ArrowRight":
        nextWidth = currentWidthRef.current + step;
        break;
      case "Home":
        nextWidth = min;
        break;
      case "End":
        nextWidth = max;
        break;
      default:
        return;
    }
    event.preventDefault();
    publishKeyboardWidth(nextWidth);
  };

  const widthAtClientX = (clientX: number, resize: PointerResize): number =>
    clamp(resize.startWidth + clientX - resize.startX, min, max);

  const flushQueuedMove = (): void => {
    const resize = pointerResizeRef.current;
    animationFrameRef.current = null;
    if (resize === null || resize.queuedWidth === null) return;
    const queuedWidth = resize.queuedWidth;
    resize.queuedWidth = null;
    resize.latestWidth = queuedWidth;
    resize.publishedWidth = queuedWidth;
    currentWidthRef.current = queuedWidth;
    onResize(queuedWidth);
  };

  const handlePointerDown = (event: PointerEvent<HTMLDivElement>): void => {
    if (disabled) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    const startWidth = clamp(currentWidthRef.current, min, max);
    pointerResizeRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth,
      latestWidth: startWidth,
      publishedWidth: startWidth,
      queuedWidth: null,
    };
  };

  const handlePointerMove = (event: PointerEvent<HTMLDivElement>): void => {
    const resize = pointerResizeRef.current;
    if (disabled || resize === null || resize.pointerId !== event.pointerId) {
      return;
    }
    resize.queuedWidth = widthAtClientX(event.clientX, resize);
    resize.latestWidth = resize.queuedWidth;
    if (animationFrameRef.current === null) {
      animationFrameRef.current = requestAnimationFrame(flushQueuedMove);
    }
  };

  const finishPointerResize = (
    event: PointerEvent<HTMLDivElement>,
  ): void => {
    const resize = pointerResizeRef.current;
    if (disabled || resize === null || resize.pointerId !== event.pointerId) {
      return;
    }

    const finalWidth = resize.queuedWidth ?? resize.latestWidth;
    resize.latestWidth = finalWidth;
    resize.queuedWidth = null;
    if (animationFrameRef.current !== null) {
      cancelAnimationFrame(animationFrameRef.current);
      animationFrameRef.current = null;
    }
    if (resize.publishedWidth !== finalWidth) {
      resize.publishedWidth = finalWidth;
      onResize(finalWidth);
    }
    currentWidthRef.current = finalWidth;
    pointerResizeRef.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
    onResizeEnd(finalWidth);
  };

  return (
    <div
      className="workspace-separator"
      role="separator"
      aria-label="Resize authoring pane"
      aria-orientation="vertical"
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={clamp(value, min, max)}
      aria-disabled={disabled || undefined}
      tabIndex={disabled ? -1 : 0}
      onKeyDown={handleKeyDown}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={finishPointerResize}
      onPointerCancel={finishPointerResize}
    />
  );
}
