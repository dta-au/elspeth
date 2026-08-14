import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FocusEvent,
} from "react";
import { useTheme } from "@/hooks/useTheme";
import { useAuthStore } from "@/stores/authStore";
import type { UserProfile } from "@/types/index";

interface UserMenuProps {
  onOpenSettings: () => void;
  onSignOut: () => void;
  /** Present only when the signed-in user is the env-flagged dev admin
   *  (/api/auth/me dev_admin); absent, the item is not rendered. */
  onOpenUserManagement?: () => void;
}

/**
 * Target for the "Help & documentation" entry. The deployment serves no
 * user-facing docs site of its own, so this points at the repository docs
 * directory named by the package metadata (pyproject [project.urls]).
 * Exported so tests pin the single honest destination.
 */
export const HELP_DOCS_URL = "https://github.com/johnm-dta/elspeth/tree/main/docs";

/** The one line that names the signed-in account: display_name when it says
 *  something, otherwise the username. Surrounding whitespace is not part of
 *  a name, so it is trimmed — which also makes an all-whitespace (or empty)
 *  display_name absence rather than a blank primary line naming nobody. */
function identityName(user: UserProfile): string {
  const displayName = user.display_name?.trim() ?? "";
  return displayName === "" ? user.username : displayName;
}

/** True only when the username is information the primary line does not
 *  already carry. Local auth sets username to the user_id and display_name
 *  is frequently the same string, so equality — not just absence — must
 *  suppress the second line. */
function showsSecondaryUsername(user: UserProfile): boolean {
  return identityName(user) !== user.username;
}

/**
 * Account dropdown in the app header. Click-outside, Escape-to-close
 * with focus return to the trigger, and Tab/Shift+Tab navigation (the
 * project convention; CommandPalette.tsx uses the same pattern).
 *
 * Role contract: this is a disclosure/popover of actions, NOT a WAI-ARIA
 * `menu` widget. The earlier `role="menu"` / `role="menuitem"` assertion
 * (with `aria-haspopup="menu"`) promised the full menu keyboard
 * contract (arrow keys, Home/End, type-ahead) which we don't implement.
 * Per the Phase 1B accessibility-audit panel finding, the correct fix
 * is to drop the menu role rather than add arrow keys: this component
 * is already a correct disclosure (Tab + Escape + focus-return).
 * Trigger uses `aria-haspopup="true"` (the "no specific popup role
 * promise" value); the dropdown is a plain `<ul>` of `<button>`
 * elements with their implicit roles.
 *
 * Item naming: "Composer preferences" rather than "Settings" because
 * the pane today only contains composer preferences — the broader
 * "Settings" framing was the UX panel's "absorb into a hub later"
 * placeholder which would mis-label a single-pane experience.
 */
export function UserMenu({
  onOpenSettings,
  onSignOut,
  onOpenUserManagement,
}: UserMenuProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const { resolvedTheme, toggleTheme } = useTheme();
  // Store-reader pattern (same as useTheme above): identity is already
  // client-side in the auth store — populated once at login/loadFromStorage
  // via GET /api/auth/me — so no props flow through AppHeader and no new
  // fetch consumer is added (elspeth-312238838a).
  const user = useAuthStore((s) => s.user);
  const themeLabel =
    resolvedTheme === "dark" ? "Switch to light theme" : "Switch to dark theme";

  // Click-outside closes
  useEffect(() => {
    if (!open) return;
    function handleMouseDown(e: MouseEvent) {
      if (
        wrapperRef.current &&
        !wrapperRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleMouseDown);
    return () => document.removeEventListener("mousedown", handleMouseDown);
  }, [open]);

  // Escape closes and returns focus to trigger
  useEffect(() => {
    if (!open) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open]);

  // Focus returns to the trigger BEFORE the item unmounts (elspeth-bcd1a9b9b3).
  // The settings dialog's focus trap saves document.activeElement when it
  // mounts and restores it on close. Closing the menu first detaches the
  // clicked item, so the browser resets focus to <body> and the trap captures
  // <body> as its restore target — leaving keyboard users nowhere on close.
  // Handing focus back here (the same move the Escape path makes) gives the
  // trap a live element to save and restore.
  const onSettings = useCallback(() => {
    triggerRef.current?.focus();
    setOpen(false);
    onOpenSettings();
  }, [onOpenSettings]);

  // Same focus-return-before-unmount move as onSettings: the user-management
  // dialog's focus trap needs a live element to save and restore.
  const onUserManagement = useCallback(() => {
    triggerRef.current?.focus();
    setOpen(false);
    onOpenUserManagement?.();
  }, [onOpenUserManagement]);

  const onSignOutClick = useCallback(() => {
    setOpen(false);
    onSignOut();
  }, [onSignOut]);

  const onThemeToggle = useCallback(() => {
    toggleTheme();
    setOpen(false);
  }, [toggleTheme]);

  // Focus leaving the menu subtree closes it (elspeth-83eb51334f): a
  // keyboard user could previously Tab past the trigger while the popup
  // stayed visually open. Only acts when relatedTarget is a real element
  // outside the wrapper — a null relatedTarget (window blur, or browsers
  // that don't focus buttons on mousedown) is left to the click-outside
  // handler so an in-menu click is never swallowed mid-flight.
  const onWrapperBlur = useCallback((e: FocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget;
    if (
      next instanceof Node &&
      wrapperRef.current !== null &&
      !wrapperRef.current.contains(next)
    ) {
      setOpen(false);
    }
  }, []);

  return (
    <div ref={wrapperRef} className="user-menu" onBlur={onWrapperBlur}>
      <button
        ref={triggerRef}
        type="button"
        aria-label="account menu"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="user-menu-trigger"
      >
        Account
      </button>
      {open && (
        <ul className="user-menu-list">
          {user !== null && (
            /* Signed-in identity header: a plain non-focusable <li> — no
               button/link — so the pinned Tab order across the action items
               is unchanged and the disclosure role contract (no role=menu)
               holds.

               The visually-hidden "Signed in as" / "username: " prefixes
               (the RecoveryPanel idiom) state the relationship the layout
               only implies: without them a screen-reader user hears two
               bare strings and nothing saying whose account this is or what
               the second line means.

               The secondary line is gated on carrying information the
               primary does not. `display_name !== null` alone was not
               enough: local auth builds UserIdentity with username ==
               user_id and display_name is a separate registration field
               that users and harnesses routinely set to the same string, so
               the row rendered "jsmith" above a muted "jsmith". An empty
               display_name likewise fell through to a blank primary line. */
            <li className="user-menu-item user-menu-identity">
              <span className="sr-only">Signed in as </span>
              <span className="user-menu-identity-name">
                {identityName(user)}
              </span>
              {showsSecondaryUsername(user) && (
                <span className="user-menu-identity-username">
                  {/* Leading space kept inside the hidden prefix: the two
                      lines are separate blocks visually, but as flat text
                      they abut, and "Jane Doeusername:" is not a sentence. */}
                  <span className="sr-only">{" username: "}</span>
                  {user.username}
                </span>
              )}
            </li>
          )}
          <li className="user-menu-item">
            <button
              type="button"
              onClick={onThemeToggle}
              aria-label={themeLabel}
              title={themeLabel}
              className="user-menu-action"
            >
              {/* No leading glyph (elspeth-66257bfab1). U+2600 / U+263E are
                  decorative pictographs from the system symbol font, not
                  members of the product icon set, and no other row in this
                  five-row text menu carries one \u2014 so the theme row read as a
                  different class of item. The label already says which way the
                  toggle goes ("Switch to light theme" / "Switch to dark
                  theme"), so dropping the span loses no meaning. */}
              {themeLabel}
            </button>
          </li>
          <li className="user-menu-item">
            <button
              type="button"
              onClick={onSettings}
              className="user-menu-action"
            >
              Composer preferences
            </button>
          </li>
          {onOpenUserManagement !== undefined && (
            <li className="user-menu-item">
              <button
                type="button"
                onClick={onUserManagement}
                className="user-menu-action"
              >
                User management
              </button>
            </li>
          )}
          <li className="user-menu-item">
            {/* One honest help entry (elspeth-8225736807): the project's
                documentation directory, opened in a new tab. Not a help
                centre — the deployment serves no docs of its own. */}
            <a
              href={HELP_DOCS_URL}
              target="_blank"
              rel="noreferrer"
              className="user-menu-action user-menu-action--link"
              onClick={() => setOpen(false)}
            >
              Help &amp; documentation
            </a>
          </li>
          <li className="user-menu-item">
            <button
              type="button"
              onClick={onSignOutClick}
              className="user-menu-action user-menu-action--danger"
            >
              Sign out
            </button>
          </li>
        </ul>
      )}
    </div>
  );
}
