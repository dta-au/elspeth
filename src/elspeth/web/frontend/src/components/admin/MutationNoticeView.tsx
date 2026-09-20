import { useEffect, useRef } from "react";
import { Button } from "@/components/ui";
import type { PersonMutation } from "./peoplePanel";

/** One section's write outcome. Refusals interrupt (alert); everything else is polite status. */
export function MutationNoticeView({ mutation }: { mutation: PersonMutation }): JSX.Element | null {
  const ref = useRef<HTMLDivElement>(null);
  const { notice, busy, refresh } = mutation;
  useEffect(() => {
    if (notice !== null) ref.current?.focus();
  }, [notice]);
  if (notice === null) return null;
  const interrupts = notice.kind === "rejected" || notice.kind === "uncertain";
  return (
    <div ref={ref} tabIndex={-1} role={interrupts ? "alert" : "status"} className={`people-notice people-notice-${notice.kind}`}>
      <span>{notice.message}</span>
      {notice.kind === "uncertain" && <Button compact disabled={busy} onClick={() => void refresh()}>Check current details</Button>}
      {notice.kind === "saved_stale" && <Button compact disabled={busy} onClick={() => void refresh()}>Retry refresh</Button>}
    </div>
  );
}
