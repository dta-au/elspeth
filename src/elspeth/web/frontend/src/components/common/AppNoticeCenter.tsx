import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

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
  const invokerRef = useRef<HTMLButtonElement>(null);
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

  const close = useCallback((restoreFocus: boolean) => {
    setOpen(false);
    if (restoreFocus) {
      invokerRef.current?.focus({ preventScroll: true });
    }
  }, []);

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
        role={primary.role}
        className={`${bannerClassName(primary.tone)} app-notice-primary`}
        data-testid="app-notice-primary"
      >
        <span className="app-notice-primary-message">{primary.content}</span>
        <span className="app-notice-primary-actions">
          {primary.action}
          {additional.length > 0 ? (
            <button
              ref={invokerRef}
              type="button"
              className="alert-banner-action app-notice-more"
              aria-expanded={open}
              aria-controls={popoverId}
              onClick={() => (open ? close(true) : setOpen(true))}
            >
              {additionalLabel}
            </button>
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
            <button
              type="button"
              className="btn btn-compact"
              onClick={() => close(true)}
            >
              Close
            </button>
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
