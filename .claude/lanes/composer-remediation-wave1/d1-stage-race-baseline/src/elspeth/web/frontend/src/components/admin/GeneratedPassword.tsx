import { type JSX, useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui";
import { useCopiedReset } from "./peoplePanel";

export interface GeneratedCredential {
  username: string;
  password: string;
  cause: "created" | "reset";
}

/**
 * The one-time password, held in component memory only.
 *
 * Focus moves here when it appears, but the region is NOT a live region: a
 * `role="status"` would have a screen reader speak the password aloud to the
 * room. The heading that receives focus names the person and what happened;
 * the value is reached deliberately.
 */
export function GeneratedPassword({ credential, onDismiss }: { credential: GeneratedCredential; onDismiss: () => void }): JSX.Element {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const headingId = useId();
  const [copy, setCopy] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    setCopy("idle");
    headingRef.current?.focus();
  }, [credential]);
  useCopiedReset(copy === "copied", () => setCopy("idle"));

  return (
    <section aria-labelledby={headingId} className="user-admin-password-banner">
      <h3 id={headingId} ref={headingRef} tabIndex={-1} className="people-password-heading">
        {credential.cause === "created" ? "Account created" : "Password reset"} for {credential.username}
      </h3>
      <p className="people-password-note">
        This password is shown once and is not stored here. Pass it on yourself: no email is sent.
        {credential.cause === "reset" && " Sessions that are already signed in stay signed in until they expire."}
      </p>
      <div className="user-admin-password-row">
        <code data-testid="generated-password" className="user-admin-password-value">{credential.password}</code>
        <Button compact onClick={() => void navigator.clipboard.writeText(credential.password).then(() => setCopy("copied"), () => setCopy("failed"))}>
          {copy === "copied" ? "Copied" : "Copy password"}
        </Button>
        <Button compact onClick={onDismiss}>Dismiss</Button>
      </div>
      {copy === "copied" && <p role="status" className="people-password-note">Password copied.</p>}
      {copy === "failed" && <p role="alert" className="user-admin-copy-failed">Copy failed. Select the password and copy it manually.</p>}
    </section>
  );
}
