import { useState, type JSX } from "react";

import { Button } from "@/components/ui";

/** Copies the stored prompt field, never its rendered representation. */
export function PromptCopyButton({ value, label, showLabel = false }: { value: string; label: string; showLabel?: boolean }): JSX.Element {
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle");

  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
      setStatus("copied");
      window.setTimeout(() => setStatus("idle"), 2000);
    } catch {
      setStatus("failed");
    }
  }

  return <>
    <Button type="button" variant="bare" className="prompt-slot-actions"
      aria-label={`Copy ${label}`} onClick={() => void copy()}>
      {status === "copied" ? "Copied" : status === "failed" ? "Copy failed" : showLabel ? `Copy ${label}` : "Copy"}
    </Button>
    <span role="status" className="visually-hidden">
      {status === "copied" ? `Copied ${label}` : status === "failed" ? `Copy failed for ${label}` : ""}
    </span>
  </>;
}
