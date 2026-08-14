import { useCallback, useEffect, useRef } from "react";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { useTheme, type Theme } from "@/hooks/useTheme";
import { Button, Input } from "@/components/ui";
import type { ComposerMode } from "@/types/api";

/**
 * Inner radio-group form. Exported standalone so component tests can render
 * it without the modal chrome; the full panel embeds it.
 *
 * Returns null before bootstrap completes — defaultMode is null until then.
 *
 * Surfaces a role="alert" region (Panel a11y F2) for failed PATCH results
 * so the write failure is announced rather than silently logging to
 * console only. Also forwards activeSessionId to setDefaultMode so the
 * banner's timing watermark is set if the user opts out from settings
 * while a session is active.
 */
interface ComposerPreferencesFormProps {
  onResetTutorialComplete?: () => void;
}

export function ComposerPreferencesForm({
  onResetTutorialComplete,
}: ComposerPreferencesFormProps = {}): JSX.Element | null {
  const defaultMode = usePreferencesStore((s) => s.defaultMode);
  const loaded = usePreferencesStore((s) => s.loaded);
  const writing = usePreferencesStore((s) => s.writing);
  const writeError = usePreferencesStore((s) => s.writeError);
  const setDefaultMode = usePreferencesStore((s) => s.setDefaultMode);
  const resetTutorial = usePreferencesStore((s) => s.resetTutorial);
  const { theme, setTheme } = useTheme();

  // TODO(hidden-jobs-settings): Add a user-settings view for hidden jobs
  // (run-bearing sessions archived from the switcher). The session switcher
  // can hide/show archived rows locally, but settings should become the
  // durable management surface for review/restore/delete policy.

  // useCallback must be unconditional (React rules of hooks); the early-return
  // for !loaded sits after the hook calls.
  const onChange = useCallback(
    async (mode: ComposerMode) => {
      const activeSessionId = useSessionStore.getState().activeSessionId;
      try {
        await setDefaultMode(mode, activeSessionId);
      } catch (err) {
        // Surfaced via writeError -> role="alert" region below.
        console.error("[preferences] setDefaultMode failed:", err);
      }
    },
    [setDefaultMode],
  );

  const onResetTutorial = useCallback(async () => {
    try {
      await resetTutorial();
      onResetTutorialComplete?.();
    } catch (err) {
      console.error("[preferences] resetTutorial failed:", err);
    }
  }, [onResetTutorialComplete, resetTutorial]);

  const onThemeChange = useCallback(
    (nextTheme: Theme) => {
      setTheme(nextTheme);
    },
    [setTheme],
  );

  if (!loaded || defaultMode === null) return null;

  return (
    <>
      <fieldset
        disabled={writing}
        aria-busy={writing}
        className="composer-preferences-fieldset"
      >
        <legend className="composer-preferences-legend">
          Default mode for new sessions
        </legend>
        <label className="composer-preferences-option">
          <Input
            type="radio"
            name="composer-default-mode"
            value="guided"
            checked={defaultMode === "guided"}
            disabled={writing}
            onChange={() => void onChange("guided")}
          />
          <span>Guided (recommended)</span>
        </label>
        <label className="composer-preferences-option">
          <Input
            type="radio"
            name="composer-default-mode"
            value="freeform"
            checked={defaultMode === "freeform"}
            disabled={writing}
            onChange={() => void onChange("freeform")}
          />
          <span>Freeform</span>
        </label>
      </fieldset>
      <fieldset className="composer-preferences-fieldset">
        <legend className="composer-preferences-legend">Theme</legend>
        <label className="composer-preferences-option">
          <Input
            type="radio"
            name="composer-theme"
            value="system"
            checked={theme === "system"}
            onChange={() => onThemeChange("system")}
          />
          <span>System</span>
        </label>
        <label className="composer-preferences-option">
          <Input
            type="radio"
            name="composer-theme"
            value="light"
            checked={theme === "light"}
            onChange={() => onThemeChange("light")}
          />
          <span>Light</span>
        </label>
        <label className="composer-preferences-option">
          <Input
            type="radio"
            name="composer-theme"
            value="dark"
            checked={theme === "dark"}
            onChange={() => onThemeChange("dark")}
          />
          <span>Dark</span>
        </label>
      </fieldset>
      {writeError !== null && (
        <div role="alert" className="composer-preferences-error">
          {writeError}
        </div>
      )}
      {/* ALWAYS offered (operator requirement: restart the tutorial from
          preferences at any time). The button was originally gated on
          tutorialCompleted, which hid it from mid-tutorial users (the wedged-
          resume escape-hatch case) and from fresh/reset users — reading as
          "the button disappeared". resetTutorial clears completion AND the
          resume fields server-side, so the next load starts a fresh Welcome;
          for a user who never started, it is a harmless no-op PATCH. */}
      <Button
        compact
        className="composer-preferences-reset"
        disabled={writing}
        onClick={() => void onResetTutorial()}
      >
        Reset tutorial
      </Button>
    </>
  );
}

interface ComposerPreferencesPanelProps {
  onClose: () => void;
  onResetTutorialComplete?: () => void;
}

/**
 * Modal wrapper around ComposerPreferencesForm. Backdrop + focus-trap +
 * Escape-close + role=dialog/aria-modal, matching the SecretsPanel pattern
 * in this codebase (src/components/settings/SecretsPanel.tsx). The project
 * does not have a generic Dialog component — this layout is the convention.
 */
export function ComposerPreferencesPanel({
  onClose,
  onResetTutorialComplete,
}: ComposerPreferencesPanelProps): JSX.Element {
  const modalRef = useRef<HTMLDivElement>(null);
  useFocusTrap(
    modalRef,
    true,
    "input[name='composer-default-mode'][value='guided']",
  );

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <>
      {/* Backdrop */}
      <div
        role="presentation"
        onClick={onClose}
        className="app-dialog-backdrop"
      />
      {/* Modal */}
      <div
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="composer-preferences-title"
        className="app-dialog settings-dialog"
      >
        <div className="secrets-panel-header">
          <h2 id="composer-preferences-title" className="secrets-panel-title">
            Composer preferences
          </h2>
          <Button
            variant="bare"
            onClick={onClose}
            aria-label="Close composer preferences panel"
            className="dialog-close"
          >
            ×
          </Button>
        </div>
        <div className="secrets-panel-body">
          <ComposerPreferencesForm
            onResetTutorialComplete={onResetTutorialComplete ?? onClose}
          />
        </div>
      </div>
    </>
  );
}
