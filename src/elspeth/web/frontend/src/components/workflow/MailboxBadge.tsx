import type { JSX } from "react";
import { Button } from "@/components/ui";
import { badgeCount, useMailboxStore } from "@/stores/mailboxStore";
import "./workflow.css";

export function MailboxBadge({ onOpen }: { onOpen: () => void }): JSX.Element | null {
  const summary = useMailboxStore((state) => state.summary);
  const count = badgeCount(summary);
  if (count === 0) return null;
  return <Button variant="bare" className="workflow-mailbox-badge" onClick={onOpen} aria-label={`Open mailbox, ${count} items need attention`}>
    Mailbox <span aria-hidden="true">{count}</span>
  </Button>;
}
