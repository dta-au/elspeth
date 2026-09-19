import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { useAuthStore } from "@/stores/authStore";
import { useMailboxStore } from "@/stores/mailboxStore";
import { InboxList, type InboxItem } from "./InboxList";
import { InspectPane } from "./InspectPane";
import { SentList } from "./SentList";
import "./workflow.css";

export function MailboxDialog({ onClose }: { onClose: () => void }): JSX.Element {
  const modalRef = useRef<HTMLDivElement>(null);
  const identityId = useAuthStore((state) => state.user?.user_id ?? "");
  const governance = useMailboxStore((state) => state.summary?.governance);
  const [folder, setFolder] = useState<"inbox" | "sent">("inbox");
  const [selected, setSelected] = useState<InboxItem | null>(null);
  const inbox = useMailboxStore((state) => state.inbox);
  const sent = useMailboxStore((state) => state.sent);
  const error = useMailboxStore((state) => state.error);
  const loadInbox = useMailboxStore((state) => state.loadInbox);
  const loadSent = useMailboxStore((state) => state.loadSent);
  const openSent = useMailboxStore((state) => state.openSent);
  useFocusTrap(modalRef, true);

  useEffect(() => {
    void loadInbox();
    function onKeyDown(event: KeyboardEvent) { if (event.key === "Escape") onClose(); }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [loadInbox, onClose]);

  function chooseFolder(next: "inbox" | "sent") {
    setSelected(null);
    setFolder(next);
    if (next === "inbox") void loadInbox();
    else void loadSent();
  }

  function onTabsKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const next = event.key === "ArrowRight" || event.key === "ArrowLeft"
      ? folder === "inbox" ? "sent" : "inbox"
      : event.key === "Home" ? "inbox" : event.key === "End" ? "sent" : null;
    if (next === null) return;
    event.preventDefault();
    chooseFolder(next);
    event.currentTarget.querySelector<HTMLButtonElement>(`[data-folder="${next}"]`)?.focus();
  }

  return <>
    <div role="presentation" className="app-dialog-backdrop" onClick={onClose} />
    <div ref={modalRef} role="dialog" aria-modal="true" aria-labelledby="workflow-mailbox-title" className="app-dialog settings-dialog settings-dialog-wide workflow-mailbox-dialog">
      <div className="secrets-panel-header">
        <h2 id="workflow-mailbox-title" className="secrets-panel-title">Mailbox</h2>
        <Button variant="bare" className="dialog-close" aria-label="Close mailbox" onClick={onClose}>×</Button>
      </div>
      <div className="secrets-panel-body">
        <div className="workflow-mailbox-tabs" role="tablist" aria-label="Mailbox folders" onKeyDown={onTabsKeyDown}>
          <Button role="tab" data-folder="inbox" tabIndex={folder === "inbox" ? 0 : -1} aria-selected={folder === "inbox"} onClick={() => chooseFolder("inbox")}>Inbox</Button>
          <Button role="tab" data-folder="sent" tabIndex={folder === "sent" ? 0 : -1} aria-selected={folder === "sent"} onClick={() => chooseFolder("sent")}>Sent</Button>
        </div>
        {governance === "off" && <p role="status">Workflow governance is off on this deployment.</p>}
        {selected !== null ? <InspectPane item={selected} onBack={() => setSelected(null)} onDone={() => setSelected(null)} />
          : folder === "inbox" ? <div role="tabpanel" aria-label="Inbox">
            {inbox === null ? <p>Loading inbox</p> : <InboxList inbox={inbox} identityId={identityId} onSelect={setSelected} />}
          </div> : <div role="tabpanel" aria-label="Sent">
            {sent === null ? <p>Loading sent requests</p> : <SentList sent={sent} identityId={identityId} onOpen={(id) => void openSent(id)} />}
          </div>}
        {selected === null && error !== null && <p role="alert">{error}</p>}
      </div>
    </div>
  </>;
}
