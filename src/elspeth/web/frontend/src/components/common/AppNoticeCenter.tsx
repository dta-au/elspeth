import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Button } from "@/components/ui";

export type AppNoticeKind =
  | "backend-unavailable"
  | "preferences"
  | "redirect"
  | "stale-build"
  | "composer-unavailable";

export interface AppNotice {
  kind: AppNoticeKind;
  role: "alert" | "status";
  content: ReactNode;
  action?: ReactNode;
  tone?: "error" | "info" | "warning" | "success";
}

const NOTICE_PRIORITY: Record<AppNoticeKind, number> = {
  "backend-unavailable": 1,
  preferences: 2,
  redirect: 3,
  "stale-build": 4,
  "composer-unavailable": 5,
};

function bannerClassName(tone: AppNotice["tone"]): string {
  return tone === undefined || tone === "error"
    ? "alert-banner"
    : `alert-banner alert-banner--${tone}`;
}

export function AppNoticeCenter({
  notices,
}: {
  notices: readonly AppNotice[];
}): JSX.Element | null {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const primaryRef = useRef<HTMLDivElement>(null);
  const invokerRef = useRef<HTMLButtonElement>(null);
  const logicalInvokerRef = useRef<HTMLButtonElement | null>(null);
  const pendingFocusRef = useRef<HTMLElement | null | undefined>(undefined);
  const popoverId = `${useId()}-app-notices`;
  const orderedNotices = useMemo(
    () =>
      notices
        .map((notice, index) => ({ notice, index }))
        .sort(
          (left, right) =>
            NOTICE_PRIORITY[left.notice.kind] -
              NOTICE_PRIORITY[right.notice.kind] || left.index - right.index,
        ),
    [notices],
  );
  const primary = orderedNotices[0]?.notice;
  const additional = orderedNotices.slice(1);
  const urgentAdditionalCount = additional.filter(
    ({ notice }) => notice.role === "alert",
  ).length;

  const close = useCallback(
    (
      restoreFocus: boolean,
      preferredFocus: HTMLElement | null = invokerRef.current,
    ) => {
      if (restoreFocus) pendingFocusRef.current = preferredFocus;
      setOpen(false);
    },
    [],
  );

  useLayoutEffect(() => {
    const primaryFallback = primaryRef.current;
    const mainFallback = document.getElementById("composer-main");
    const preferred = pendingFocusRef.current;
    let target: HTMLElement | null = null;
    if (preferred !== undefined) {
      pendingFocusRef.current = undefined;
      target =
        preferred?.isConnected === true
          ? preferred
          : primaryFallback?.isConnected === true
            ? primaryFallback
            : mainFallback;
    } else if (
      logicalInvokerRef.current !== null &&
      !logicalInvokerRef.current.isConnected &&
      (document.activeElement === document.body ||
        document.activeElement === document.documentElement)
    ) {
      logicalInvokerRef.current = null;
      target =
        primaryFallback?.isConnected === true ? primaryFallback : mainFallback;
    }
    if (target instanceof HTMLElement && target.isConnected) {
      target.focus({ preventScroll: true });
      if (target === invokerRef.current) {
        logicalInvokerRef.current = invokerRef.current;
      }
    }
  });

  useEffect(() => {
    if (!open) return undefined;
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        close(true);
      }
    }
    function handleClick(event: MouseEvent) {
      if (
        event.target instanceof Node &&
        !rootRef.current?.contains(event.target)
      ) {
        close(true);
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("click", handleClick);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("click", handleClick);
    };
  }, [close, open]);

  useEffect(() => {
    if (additional.length === 0 && open) {
      close(true);
    }
  }, [additional.length, close, open]);

  if (primary === undefined) return null;

  const additionalLabel = `${additional.length} more ${
    additional.length === 1 ? "notice" : "notices"
  }`;
  const urgentSummary = `${urgentAdditionalCount} additional urgent ${
    urgentAdditionalCount === 1 ? "notice is" : "notices are"
  } available.`;

  return (
    <div ref={rootRef} className="app-notice-center">
      <div
        ref={primaryRef}
        role={primary.role}
        tabIndex={-1}
        className={`${bannerClassName(primary.tone)} app-notice-primary`}
        data-testid="app-notice-primary"
      >
        <span className="app-notice-primary-message">{primary.content}</span>
        <span className="app-notice-primary-actions">
          {primary.action === undefined ? null : (
            // NOT a control: primary.action is a caller-supplied node that is
            // already an interactive, .alert-banner-action-styled Button
            // (every producer in App.tsx passes one). This span's onClick is
            // a bubbling shim that closes the notice centre after the inner
            // action activates — wrapping it in a Button would nest
            // button-in-button.
            <span
              className="app-notice-primary-action"
              onClick={() =>
                close(
                  true,
                  document.activeElement instanceof HTMLElement
                    ? document.activeElement
                    : null,
                )
              }
            >
              {primary.action}
            </span>
          )}
          {additional.length > 0 ? (
            <Button
              ref={invokerRef}
              variant="bare"
              className="alert-banner-action app-notice-more"
              aria-expanded={open}
              aria-controls={popoverId}
              onFocus={() => {
                logicalInvokerRef.current = invokerRef.current;
              }}
              onClick={() => (open ? close(true) : setOpen(true))}
            >
              {additionalLabel}
            </Button>
          ) : null}
        </span>
      </div>

      {urgentAdditionalCount > 0 ? (
        <span className="sr-only" role="alert">
          {urgentSummary}
        </span>
      ) : null}

      {open ? (
        <section
          id={popoverId}
          className="app-notice-popover"
          role="region"
          aria-label="All notices"
        >
          <header className="app-notice-popover-header">
            <h2>Notifications</h2>
            <Button compact onClick={() => close(true)}>
              Close
            </Button>
          </header>
          <div className="app-notice-list">
            {orderedNotices.map(({ notice, index }) => (
              <div
                key={`${notice.kind}-${index}`}
                role={notice.role}
                aria-live="off"
                className={`${bannerClassName(notice.tone)} app-notice-item`}
                data-testid="app-notice-additional"
              >
                <span className="app-notice-item-message">{notice.content}</span>
                {notice.action === undefined ? null : (
                  // Same shape as the primary action's wrapper above: the
                  // caller-supplied node inside is the styled control; this
                  // span only closes the popover on activation via bubbling.
                  <span
                    className="app-notice-item-action"
                    onClick={() => close(true)}
                  >
                    {notice.action}
                  </span>
                )}
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}
