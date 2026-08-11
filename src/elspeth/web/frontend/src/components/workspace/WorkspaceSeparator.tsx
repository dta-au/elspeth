import {
  type KeyboardEvent,
  type PointerEvent,
  useCallback,
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

interface SeparatorContract {
  min: number;
  max: number;
  disabled: boolean;
  onResize: (value: number) => void;
  onResizeEnd: (finalWidth: number) => void;
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
  const contractRef = useRef<SeparatorContract>({
    min,
    max,
    disabled,
    onResize,
    onResizeEnd,
  });

  useLayoutEffect(() => {
    contractRef.current = { min, max, disabled, onResize, onResizeEnd };
  }, [disabled, max, min, onResize, onResizeEnd]);

  useLayoutEffect(() => {
    const resize = pointerResizeRef.current;
    if (resize !== null) {
      resize.latestWidth = clamp(resize.latestWidth, min, max);
      resize.queuedWidth =
        resize.queuedWidth === null
          ? null
          : clamp(resize.queuedWidth, min, max);
      currentWidthRef.current = resize.latestWidth;
      if (animationFrameRef.current !== null) {
        cancelAnimationFrame(animationFrameRef.current);
        animationFrameRef.current = null;
      }
      return;
    }
    currentWidthRef.current = clamp(value, min, max);
  }, [disabled, max, min, value]);

  const settlePointerResize = useCallback(
    (pointerId: number, releaseTarget: HTMLDivElement | null): void => {
      const resize = pointerResizeRef.current;
      if (resize === null || resize.pointerId !== pointerId) return;

      const contract = contractRef.current;
      const finalWidth = clamp(
        resize.queuedWidth ?? resize.latestWidth,
        contract.min,
        contract.max,
      );
      if (animationFrameRef.current !== null) {
        cancelAnimationFrame(animationFrameRef.current);
        animationFrameRef.current = null;
      }
      currentWidthRef.current = finalWidth;
      pointerResizeRef.current = null;

      if (releaseTarget !== null) {
        let captured = false;
        try {
          captured = releaseTarget.hasPointerCapture(pointerId);
        } catch {
          // Capture may already have been released by the browser.
        }
        if (captured) {
          try {
            releaseTarget.releasePointerCapture(pointerId);
          } catch {
            // A racing lostpointercapture event owns the release now.
          }
        }
      }

      if (resize.publishedWidth !== finalWidth) {
        contract.onResize(finalWidth);
      }
      contract.onResizeEnd(finalWidth);
    },
    [],
  );

  useEffect(
    () => () => {
      const resize = pointerResizeRef.current;
      if (resize !== null) {
        // Lifecycle choice: an unmounted separator settles its last valid
        // width exactly once; no detached pointer session or RAF survives.
        settlePointerResize(resize.pointerId, null);
      } else if (animationFrameRef.current !== null) {
        cancelAnimationFrame(animationFrameRef.current);
        animationFrameRef.current = null;
      }
    },
    [settlePointerResize],
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
    clamp(
      resize.startWidth + clientX - resize.startX,
      contractRef.current.min,
      contractRef.current.max,
    );

  const flushQueuedMove = (): void => {
    const resize = pointerResizeRef.current;
    animationFrameRef.current = null;
    const contract = contractRef.current;
    if (
      resize === null ||
      resize.queuedWidth === null ||
      contract.disabled
    ) {
      return;
    }
    const queuedWidth = clamp(
      resize.queuedWidth,
      contract.min,
      contract.max,
    );
    resize.queuedWidth = null;
    resize.latestWidth = queuedWidth;
    resize.publishedWidth = queuedWidth;
    currentWidthRef.current = queuedWidth;
    contract.onResize(queuedWidth);
  };

  const handlePointerDown = (event: PointerEvent<HTMLDivElement>): void => {
    if (
      disabled ||
      event.button !== 0 ||
      !event.isPrimary ||
      pointerResizeRef.current !== null
    ) {
      return;
    }
    event.preventDefault();
    const startWidth = clamp(currentWidthRef.current, min, max);
    pointerResizeRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth,
      latestWidth: startWidth,
      publishedWidth: startWidth,
      queuedWidth: null,
    };
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Without capture, a pointer leaving this element has no guaranteed
      // terminal event. Abort now so the failed attempt cannot wedge resizing.
      pointerResizeRef.current = null;
    }
  };

  const handlePointerMove = (event: PointerEvent<HTMLDivElement>): void => {
    const resize = pointerResizeRef.current;
    if (
      resize === null ||
      resize.pointerId !== event.pointerId ||
      contractRef.current.disabled
    ) {
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
    settlePointerResize(event.pointerId, event.currentTarget);
  };

  const handleLostPointerCapture = (
    event: PointerEvent<HTMLDivElement>,
  ): void => {
    settlePointerResize(event.pointerId, null);
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
      onLostPointerCapture={handleLostPointerCapture}
    />
  );
}
