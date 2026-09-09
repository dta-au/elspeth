import { useEffect, useId, useRef, type ReactNode } from "react";
import { Button } from "@/components/ui";
import { useFocusTrap } from "@/hooks/useFocusTrap";

interface ConfirmDialogProps {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: "default" | "danger";
  onConfirm: () => void;
  onCancel: () => void;
  /**
   * Optional structured content rendered between the message and the
   * action buttons (e.g. the pre-run egress summary list and its
   * "don't ask again" opt-out). Interactive children participate in the
   * focus trap automatically.
   */
  children?: ReactNode;
}

/**
 * Styled replacement for window.confirm().
 *
 * Focus-trapped modal with keyboard support (Escape to cancel,
 * Enter on confirm button). Focus returns to the trigger element
 * on close via useFocusTrap's restore-on-unmount behaviour.
 */
export function ConfirmDialog({
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "default",
  onConfirm,
  onCancel,
  children,
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const id = useId();
  const titleId = `${id}-confirm-dialog-title`;
  const messageId = `${id}-confirm-dialog-message`;

  // Focus trap with initial focus on the confirm button.
  // On unmount, focus restores to the element that was focused before the dialog.
  useFocusTrap(dialogRef, true, ".confirm-dialog-confirm-btn");

  // Escape key closes
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onCancel]);

  return (
    <>
      <div
        className="confirm-dialog-backdrop"
        onClick={onCancel}
        role="presentation"
      />
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={messageId}
        className="confirm-dialog"
      >
        <header className="confirm-dialog-header">
          <h2 id={titleId} className="confirm-dialog-title">
            {title}
          </h2>
        </header>
        <div className="confirm-dialog-body">
          <p id={messageId} className="confirm-dialog-message">
            {message}
          </p>
          {children}
        </div>
        <footer className="confirm-dialog-actions">
          <Button onClick={onCancel} className="confirm-dialog-btn">
            {cancelLabel}
          </Button>
          <Button
            variant={variant === "danger" ? "danger" : "primary"}
            onClick={onConfirm}
            className="confirm-dialog-btn confirm-dialog-confirm-btn"
          >
            {confirmLabel}
          </Button>
        </footer>
      </div>
    </>
  );
}
