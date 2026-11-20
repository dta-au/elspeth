import {
  createContext,
  type JSX,
  useContext,
  useLayoutEffect,
  useRef,
} from "react";

import { Button, Icon } from "@/components/ui";
import type { CollapsedAuthoringStatus } from "./useCollapsedAuthoringStatus";

/** The shortcut as the shortcuts sheet, the palette and the tooltip show it. */
export const AUTHORING_TOGGLE_SHORTCUT = "Ctrl+\\";

/** What the hide/show toggle needs from ComposerWorkspace. */
export interface AuthoringToggleState {
  /** The pane is collapsed (desktop layouts only; narrow never collapses). */
  collapsed: boolean;
  /** The authoring pane's title (ComposerWorkspace's authoringTitle). */
  title: string;
  status: CollapsedAuthoringStatus;
  toggle: () => void;
  /** Called when an instance unmounts while it holds focus (the layout went
   *  narrow, or the state flipped), so the workspace can put focus somewhere
   *  visible instead of leaving it on the document body. */
  focusDropped: () => void;
}

const AuthoringToggleContext = createContext<AuthoringToggleState | null>(null);

/** ComposerWorkspace provides null under 960px: the Compose / Pipeline tabs
 *  switch views there, so no toggle renders. */
export const AuthoringToggleProvider = AuthoringToggleContext.Provider;

/** "Hide conversation" / "Show conversation"; a host that titles the pane
 *  differently (the tutorial's "Run results") gets its own noun. */
export function authoringToggleLabel(collapsed: boolean, title: string): string {
  const noun = title.charAt(0).toLowerCase() + title.slice(1);
  return `${collapsed ? "Show" : "Hide"} ${noun}`;
}

/** The toggle carries data-authoring-toggle="hide" | "show"; ComposerWorkspace
 *  finds the mounted instance by it. */
export const AUTHORING_TOGGLE_SELECTOR = "[data-authoring-toggle]";
export const COLLAPSED_STATUS_ID = "workspace-collapsed-status";

function badgeText(count: number): string {
  return count > 9 ? "9+" : String(count);
}

interface AuthoringPaneToggleProps {
  /** Which title bar hosts this instance. The conversation's title bar shows
   *  the Hide control while the pane is open; the pipeline's title bar shows
   *  the Show control while it is collapsed. Both sit at the left end of
   *  their bar, so the control does not move when the state flips. */
  placement: "authoring" | "artifact";
}

/**
 * The chat pane's hide/show control. One component, two hosts (the
 * conversation's title bar and the pipeline's), so exactly one instance is
 * mounted in either state. ComposerWorkspace moves focus to whichever
 * instance is mounted after a toggle.
 */
export function AuthoringPaneToggle({
  placement,
}: AuthoringPaneToggleProps): JSX.Element | null {
  const state = useContext(AuthoringToggleContext);
  if (state === null) return null;
  if (state.collapsed !== (placement === "artifact")) return null;
  return <MountedToggle state={state} />;
}

function MountedToggle({
  state,
}: {
  state: AuthoringToggleState;
}): JSX.Element {
  const { collapsed, title, status, toggle, focusDropped } = state;
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  // Unmount cleanup of a layout effect runs before React removes the
  // button, so it can still see whether the button held focus. (A deps
  // change would run it after the removal, hence the separate component;
  // focusDropped is a stable callback.)
  useLayoutEffect(() => {
    const button = buttonRef.current;
    return () => {
      if (button !== null && document.activeElement === button) {
        focusDropped();
      }
    };
  }, [focusDropped]);

  const label = authoringToggleLabel(collapsed, title);
  const unread = collapsed ? (status.unreadCount ?? 0) : 0;
  const dotTone =
    collapsed && unread === 0 && status.tone !== "neutral" ? status.tone : null;
  const statusInTooltip = collapsed && (unread > 0 || dotTone !== null);
  const tooltip = `${label} (${AUTHORING_TOGGLE_SHORTCUT})${
    statusInTooltip ? `\n${status.text}` : ""
  }`;

  return (
    <>
      <Button
        ref={buttonRef}
        variant="bare"
        className="workspace-pane-toggle"
        data-authoring-toggle={collapsed ? "show" : "hide"}
        title={tooltip}
        aria-label={label}
        aria-expanded={!collapsed}
        aria-controls="workspace-authoring-pane"
        aria-describedby={collapsed ? COLLAPSED_STATUS_ID : undefined}
        aria-keyshortcuts="Control+Backslash"
        onClick={toggle}
      >
        <Icon
          name={collapsed ? "chevrons-right" : "chevrons-left"}
          className="workspace-pane-toggle-icon"
        />
        {/* Decorative: the status node below carries the same fact to
            assistive tech, through aria-describedby and its live region. */}
        {unread > 0 && (
          <span
            className="workspace-pane-toggle-badge workspace-pane-toggle-badge--count"
            aria-hidden="true"
          >
            {badgeText(unread)}
          </span>
        )}
        {dotTone !== null && (
          <span
            className="workspace-pane-toggle-badge workspace-pane-toggle-badge--dot"
            data-tone={dotTone}
            aria-hidden="true"
          />
        )}
      </Button>
      {collapsed && (
        <span
          id={COLLAPSED_STATUS_ID}
          className="visually-hidden"
          role="status"
          data-tone={status.tone}
        >
          {status.text}
        </span>
      )}
    </>
  );
}
